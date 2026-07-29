#!/usr/bin/env python3
"""Submit-time close-linkage consistency pre-flight (engine-template #361).

GitHub auto-closes an issue from any `close`/`fixes`/`resolves #N` keyword — including one buried in prose — in
a pull request's body OR an integrated commit message. So a PR gets accidentally set to close an issue it only
*partly* addresses, and the engine then reports a wrong backlog. This has bitten four times, each caught only by
the operator by eye. This pre-flight is the honest mechanical guard the operator authorized: run at **submit,
before the draft PR is marked ready**, it compares the set the PR **will** close against what the PR **declares**
it closes, and surfaces any contradiction in the Review record the operator reads at the merge.

**Not a check, not a gate.** The comparison is mechanical, but its delivery rides the AI-authored Review record,
so it inherits Review's posture-truthfulness tier ("the engine's own account; your approval is the real gate").
There is no `.engine/check/*.json`, no CI suite entry, no merge gate. The orchestrator invokes this at submit and
folds its lines into Review — the same shape `spec_referent.py review-steps` already uses (a tool whose output
the orchestrator drops into Review verbatim).

What it reads (machine-decidable facts only, no intent-guessing):
  - the set the PR **will** close = GitHub's computed `closingIssuesReferences` (the body-keyword linkage,
    read via `gh pr view --json closingIssuesReferences`, gh >= 2.72.0, with a `gh api graphql` fallback) **plus**
    the closing keywords in the **integrated commit messages** (`git log <base>..<head>`), which that field does
    not reflect and which only the submit-time orchestrator holds;
  - what the PR **declares** — its own structured Scope/Out-of-scope: a top-of-body `Closes #N` deliberate-close
    line, and a canonical `Part of #N` dependency declaration.

Two contradictions are decidable without reading intent, and one bound is named:
  - **scope-contradiction** — the PR will close #N while its scope declares it only "Part of #N";
  - **comma-trap** — `Closes #1, #2` links only #1, leaving #2 silently open;
  - **cross-repo (out of reach)** — a `owner/repo#N` close has no local Scope line to adjudicate, so it is
    surfaced-and-named, never silently passed and never defanged.

Acting is **detect-and-surface, never silent-and-unilateral**: the default is to *surface* a plain line;
only an **unambiguously-accidental**, **body-sourced** keyword (scope declares "Part of #N", no deliberate close
line, and the honored occurrence is uniquely locatable) is **neutralized** — a minimal, keyword-only defang of
the engine's own PR body, byte-identical everywhere else, **never** a narrative rewrite and **never** a read or
edit of product scope. Any defang is **disclosed** in Review; a wanted close wrongly neutralized is a disclosed,
operator-recoverable miss. When the honored occurrence cannot be uniquely located and reconciled against GitHub's
own set, or the close is commit-sourced (a body edit cannot neutralize it), the pre-flight **surfaces** instead of
defanging — a false "I removed…" would break the one informed-consent bound this path has.

Fail-closed: if the will-close set cannot be read at submit (a stale `gh`, a missing `issues: read` sub-scope on a
private repo, an unreachable host), the pre-flight emits the **could-not-read** line pointing the operator to
GitHub's own "will close" list — NEVER a false "nothing will close".

This tool depends on `.github/pull_request_template.md` keeping its exact `## Scope` and `## Out of scope`
headings (the ones `validate.section_blocks` keys on): a rename silently blinds the `Part of #N` read, and the
fail-safe direction is then *surface* (an unclassifiable contradiction is surfaced, never defanged). It performs
**no writes** — it emits text (the Review lines, and for the accidental-body case the exact defanged body); the
orchestrator applies the body via `gh pr edit --body-file` and pastes the disclosure.

Run the demo (faked gh/git, real logic — no real PR, no token):
  uv run --directory .engine -- python tools/close_linkage_preflight.py demo
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402 — for section_blocks (the PR-body section parser)

# The closing-keyword alternation — GitHub's common close/fix/resolve forms, kept SELF-CONTAINED here (a
# standalone submit-time tool must not couple to another module for this; the spec_referent.py
# self-contained-regex precedent). The `[:\s]+` separator and keyword set are a **best-effort approximation**
# of GitHub's rule: a commit-message close GitHub honors but this misses would degrade toward a false clean, so
# keep this aligned with GitHub's keywords.
_KW = r"close[sd]?|fix(?:es|ed)?|resolve[sd]?"

# One closing REFERENCE-LIST after a keyword: `#N`, an `owner/repo#N` cross-repo ref, or a comma-run of them
# (`Closes #1, #2`). The whole list is captured so the comma-trap under-link can be detected. The leading \b
# keeps it from matching inside another word ("discloses #7").
_CLOSE_LIST_RE = re.compile(
    rf"\b(?:{_KW})\b[:\s]+((?:[\w.-]+/[\w.-]+)?#\d+(?:\s*,\s*(?:[\w.-]+/[\w.-]+)?#\d+)*)",
    re.IGNORECASE,
)

# A single `owner/repo#N` or `#N` reference inside a captured list.
_REF_RE = re.compile(r"(?:(?P<slug>[\w.-]+/[\w.-]+)#|#)(?P<num>\d+)")

# A DELIBERATE close line: a line whose FIRST token is a closing keyword directly followed by a bare `#N` (the
# template's "one `Closes #N` line" convention). This is how an *intended* close is told from a keyword *buried*
# mid-sentence — a location read, not an intent guess.
_DELIBERATE_LINE_RE = re.compile(rf"^\s*(?:{_KW})\b[:\s]+#(\d+)\s*$", re.IGNORECASE)

# A DELIBERATE cross-repo close line: line-leading `<keyword> owner/repo#N`. Only a deliberate, line-leading
# cross-repo close is surfaced as out of reach — a buried/fenced/quoted `owner/repo#N` mention is not (GitHub
# does not auto-close cross-repo anyway, so an example must never raise a false out-of-reach line).
_DELIBERATE_CROSS_RE = re.compile(rf"^\s*(?:{_KW})\b[:\s]+([\w.-]+/[\w.-]+)#(\d+)\s*$", re.IGNORECASE)

# A canonical `Part of #N` dependency declaration (case-insensitive, comma-runs allowed) written in
# Scope/Out-of-scope. Named in `.github/pull_request_template.md`.
_PART_OF_RE = re.compile(r"\bpart of\b[:\s]+((?:#\d+)(?:\s*,\s*#\d+)*)", re.IGNORECASE)

# The PR-body sections whose declarations the pre-flight reads. Coupled to the template headings by name.
_DECLARE_SECTIONS = ("Scope", "Out of scope")

USER_AGENT = "engine-close-linkage-preflight"


class PreflightUnavailable(Exception):
    """The will-close set could not be read at submit (a stale `gh`, a missing `issues: read` scope, an
    unreachable host, an unexpected shape). NEVER swallowed as "nothing will close": the caller renders the
    could-not-read line, which points the operator to GitHub's own "will close" list. Distinct from a genuine
    clean read that finds no contradiction (which produces no line at all)."""


# ---- pure parse (no I/O — every branch is unit-tested) ---------------------------------------------

def _refs_in_list(captured: str) -> list:
    """Every `(slug_or_None, number)` in a captured closing reference-list, in order. `slug` is the
    `owner/repo` of a cross-repo ref, or None for a same-repo `#N`."""
    out = []
    for m in _REF_RE.finditer(captured):
        out.append((m.group("slug"), int(m.group("num"))))
    return out


def parse_close_runs(text: str) -> list:
    """Every closing keyword's reference-RUN in `text`, as `[[(slug_or_None, number), ...], ...]`. GitHub honors
    only the FIRST reference of a run (`Closes #1, #2` closes only #1); the rest are the comma-trap leftovers. A
    same-repo ref has slug None; a cross-repo ref keeps its `owner/repo` slug."""
    return [_refs_in_list(m.group(1)) for m in _CLOSE_LIST_RE.finditer(text)]


def body_local_closes(body: str) -> set:
    """The same-repo issue numbers named by any closing keyword in the body — the code-fence-agnostic candidate
    set, later intersected with GitHub's honored `closingIssuesReferences` (which drops fenced/quoted hits)."""
    return {num for run in parse_close_runs(body) for (slug, num) in run if slug is None}


def comma_trap_leftovers(runs: list, honored_local: set) -> list:
    """The same-repo numbers that trail a comma-listed close whose HEAD GitHub actually honored — a real
    `Closes #1, #2` where #1 closes but #2 (not itself honored) silently stays open. Gating on the honored head
    is the reconciliation that keeps a fenced / quoted / HTML-comment / prose `Closes #1, #2` *example* (whose
    head GitHub honored nothing of) from firing a false 'will stay open' warning. Order-preserving, de-duped."""
    out, seen = [], set()
    for run in runs:
        if len(run) < 2:
            continue
        head_slug, head_num = run[0]
        if head_slug is not None or head_num not in honored_local:
            continue                                   # the run's head isn't a real, honored same-repo close
        for slug, num in run[1:]:
            if slug is None and num not in honored_local and num not in seen:
                seen.add(num)
                out.append(num)
    return out


def deliberate_cross_closes(body: str) -> list:
    """The cross-repo `owner/repo#N` closes written as a DELIBERATE, line-leading keyword — the ones worth
    naming as out of reach. A buried / fenced / quoted `owner/repo#N` is NOT surfaced (GitHub does not
    auto-close cross-repo, so an example mention must not raise a false line). Order-preserving, de-duped."""
    out, seen = [], set()
    for line in body.splitlines():
        m = _DELIBERATE_CROSS_RE.match(line)
        if m:
            ref = f"{m.group(1)}#{m.group(2)}"
            if ref not in seen:
                seen.add(ref)
                out.append(ref)
    return out


def deliberate_closes(body: str) -> set:
    """The same-repo numbers a body declares closed with a DELIBERATE, line-leading `Closes #N` (the template
    convention). These are intended closes — a `Part of #N` alongside one of these is a genuine contradiction to
    *surface*, not an accidental keyword to defang."""
    return {int(m.group(1)) for line in body.splitlines() if (m := _DELIBERATE_LINE_RE.match(line))}


def part_of_declarations(body: str) -> set:
    """The same-repo numbers the PR declares itself only `Part of #N` in its Scope / Out of scope sections.
    Read through `validate.section_blocks`, which keys on the exact `## Scope` / `## Out of scope` headings — a
    template rename silently empties this (the fail-safe direction: an unclassifiable contradiction is surfaced,
    never defanged)."""
    blocks = validate.section_blocks(body or "")
    out = set()
    for name in _DECLARE_SECTIONS:
        section = blocks.get(name, "")
        for m in _PART_OF_RE.finditer(section):
            out.update(int(x) for x in re.findall(r"#(\d+)", m.group(1)))
    return out


def commit_will_close(messages: list) -> tuple:
    """`(honored, trap)` for the integrated commit messages, which `closingIssuesReferences` does not reflect.
    `honored` is the same-repo numbers a commit will actually close — the FIRST same-repo ref of each closing
    run; `trap` is the same-repo comma-trap leftovers (a commit `Closes #1, #2` closes only #1, so #2 silently
    stays open — the same failure mode the body path catches). A run led by a cross-repo ref closes nothing (a
    cross-repo commit close cannot be neutralized by a body edit, and GitHub does not auto-close cross-repo), so
    its refs are ignored — never counted as a will-close or a trap."""
    honored, trap, seen = set(), [], set()
    for msg in messages:
        for run in parse_close_runs(msg or ""):
            head_slug, head_num = run[0]
            if head_slug is not None:
                continue                               # cross-repo head: nothing closes here, no trap
            honored.add(head_num)
            for slug, num in run[1:]:
                if slug is None and num not in seen:
                    seen.add(num)
                    trap.append(num)
    return honored, trap


def defang_body(body: str, number: int) -> "str | None":
    """The body with the single accidental closing keyword for `#N` neutralized — the keyword+separator removed,
    the `#N` reference KEPT (so `... builds on Closes #274 ...` -> `... builds on #274 ...`). Deterministic and
    BYTE-IDENTICAL everywhere else. Returns None (-> the caller surfaces instead) when the occurrence is not
    exactly one, so a defang never edits the wrong (e.g. code-fenced) occurrence or misreports what it removed."""
    spans = []
    for m in _CLOSE_LIST_RE.finditer(body):
        refs = _refs_in_list(m.group(1))
        # only a lone same-repo `keyword #N` is defang-eligible; a comma-run or a cross-repo ref is not a clean
        # accidental single close, so it is left for the surface path.
        if refs == [(None, number)]:
            hash_pos = body.index("#", m.start())     # start of `#N`, within this match
            spans.append((m.start(), hash_pos))
    if len(spans) != 1:
        return None                                    # zero, or ambiguous multiple -> surface, never guess
    start, hash_pos = spans[0]
    return body[:start] + body[hash_pos:]              # drop `keyword<sep>`, keep `#N`; identical elsewhere


# ---- the classification (pure; produces the operator lines + an optional defang) -------------------

def _line_scope_contradiction(n: int) -> str:
    return (f"This PR is set to close #{n}, but its scope says this PR is only part of the work for #{n} — "
            f"the closing line needs a small edit before you merge.")


def _line_defang_disclosure(n: int) -> str:
    return (f"I removed an accidental closing keyword that would have closed #{n}; this change is only part "
            f"of #{n}.")


def _line_comma_trap(nums: list) -> str:
    which = ", ".join(f"#{n}" for n in nums)
    tail = "it will stay open" if len(nums) == 1 else "they will stay open"
    return (f"The closing line lists more than one issue, but only the first one closes on merge — {which} "
            f"will not close, so {tail} after merge. Give each its own closing line if you meant to close it.")


def _line_cross_repo(refs: list) -> str:
    which = ", ".join(refs)
    return (f"This PR is set to close {which} in another repository, which I can't check against this "
            f"project's plan — confirm on GitHub that you meant to close it.")


def _line_could_not_read() -> str:
    return ("I couldn't check what this PR will close before submitting — open the PR on GitHub and confirm its "
            "“will close” list before you merge.")


def classify(*, body: str, honored_local: set, commit_honored: set, commit_trap: "list | None" = None,
             unavailable: bool = False) -> dict:
    """Compare the will-close set against the PR's own declarations and produce the operator lines + an optional
    defang. Pure. `honored_local` is the same-repo body linkage GitHub actually computed
    (`closingIssuesReferences` numbers, already code-fence-agnostic); `commit_honored`/`commit_trap` are the
    integrated-commit closes and their comma-trap leftovers (from `commit_will_close`). `unavailable=True`
    short-circuits to the could-not-read line (never a false clean).

    Returns {"lines": [...], "defang": None | {"number", "new_body", "disclosure"}}."""
    if unavailable:
        return {"lines": [_line_could_not_read()], "defang": None}

    commit_trap = commit_trap or []
    runs = parse_close_runs(body)
    declared_part_of = part_of_declarations(body)
    declared_close = deliberate_closes(body)
    lines: list = []
    defang = None

    # A same-repo body close is HONORED only when GitHub's own set confirms it — this drops a keyword sitting in
    # a code fence, block-quote, or HTML comment (which GitHub ignores but the raw-text regex would see).
    honored_body = body_local_closes(body) & honored_local
    will_close_local = honored_body | commit_honored

    # scope-contradictions: an issue the PR will close while its scope declares it only "Part of #N".
    for n in sorted(will_close_local & declared_part_of):
        accidental_body = (n in honored_body and n not in declared_close
                           and n not in commit_honored)    # a body-sourced, non-deliberate, non-commit close
        new_body = defang_body(body, n) if accidental_body else None
        if new_body is not None and defang is None:
            # unambiguously-accidental and uniquely locatable -> minimal keyword-only defang, disclosed.
            defang = {"number": n, "new_body": new_body, "disclosure": _line_defang_disclosure(n)}
            lines.append(_line_defang_disclosure(n))
        else:
            # deliberate-close-alongside-part-of, commit-sourced (a body edit can't neutralize it), or an
            # ambiguous body occurrence -> surface, never a silent or misreported change.
            lines.append(_line_scope_contradiction(n))

    # comma-trap under-link (independent of scope): a `Closes #1, #2` whose head is honored but whose trailers
    # will not close. Body traps are reconciled against the honored head; commit traps are the deterministic
    # first-of-run rule. De-dup across both, order-preserving.
    trap = comma_trap_leftovers(runs, honored_local)
    for n in commit_trap:
        if n not in trap:
            trap.append(n)
    if trap:
        lines.append(_line_comma_trap(trap))

    # cross-repo closes are out of reach — a DELIBERATE line-leading cross-repo close is surfaced-and-named,
    # never defanged; a buried/quoted example is not surfaced.
    cross = deliberate_cross_closes(body)
    if cross:
        lines.append(_line_cross_repo(cross))

    return {"lines": lines, "defang": defang}


def render(result: dict) -> str:
    """The plain Review-block text (the operator lines joined), or "" when there is no contradiction — a null
    result produces NO line and is not part of Review's non-empty requirement."""
    return "\n".join(result["lines"])


# ---- the gh / git boundary (the only I/O; the runner is injectable for demo + tests) ---------------

def _default_runner(cmd: list) -> tuple:
    """Run a local command, returning (returncode, stdout, stderr). Never raises for a non-zero exit — the
    caller reads the code. A missing binary / timeout / OS error surfaces as returncode 127 so it degrades to
    the could-not-read line rather than crashing the submit."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        return out.returncode, out.stdout, out.stderr
    except Exception as exc:  # noqa: BLE001 — a missing gh/git, a timeout, an OS error all degrade to unreadable
        return 127, "", str(exc)


def read_will_close(pr, runner=_default_runner) -> set:
    """The same-repo issue numbers GitHub computes this PR will close, from `gh pr view <pr> --json
    closingIssuesReferences`, with a `gh api graphql` fallback when the flag is unsupported (gh < 2.72.0).
    RAISES PreflightUnavailable when neither read succeeds — NEVER read as an empty set (a false "nothing will
    close")."""
    code, out, _ = runner(["gh", "pr", "view", str(pr), "--json", "closingIssuesReferences"])
    if code == 0:
        try:
            data = json.loads(out or "{}")
            refs = data.get("closingIssuesReferences")
            if isinstance(refs, list):
                return {int(r["number"]) for r in refs if isinstance(r, dict) and "number" in r}
        except (ValueError, KeyError, TypeError):
            pass  # a 0-exit with an unexpected shape falls through to the graphql fallback, then to RAISE
    # fallback: the GraphQL projection (older gh, or the flag unsupported). Best-effort; unreadable -> RAISE.
    numbers = _graphql_will_close(pr, runner)
    if numbers is None:
        raise PreflightUnavailable(
            f"could not read the will-close set for PR #{pr} (gh unavailable, unsupported, or missing scope)")
    return numbers


def _graphql_will_close(pr, runner) -> "set | None":
    """The `gh api graphql` fallback for `closingIssuesReferences` — same-repo numbers, or None when unreadable.
    Needs `GITHUB_REPOSITORY` (owner/repo) in the environment, as the submit context carries."""
    slug = os.environ.get("GITHUB_REPOSITORY")
    if not slug or "/" not in slug:
        return None
    owner, name = slug.split("/", 1)
    query = ("query($o:String!,$n:String!,$p:Int!){repository(owner:$o,name:$n){"
             "pullRequest(number:$p){closingIssuesReferences(first:50){nodes{number}}}}}")
    code, out, _ = runner(["gh", "api", "graphql", "-f", f"query={query}",
                           "-F", f"o={owner}", "-F", f"n={name}", "-F", f"p={pr}"])
    if code != 0:
        return None
    try:
        nodes = (json.loads(out)["data"]["repository"]["pullRequest"]
                 ["closingIssuesReferences"]["nodes"])
        return {int(node["number"]) for node in nodes if "number" in node}
    except (ValueError, KeyError, TypeError):
        return None


def read_body(pr, runner=_default_runner) -> str:
    """The PR body text via `gh pr view <pr> --json body`. RAISES PreflightUnavailable on a failed read (never
    read as an empty body, which would silently find no declarations)."""
    code, out, _ = runner(["gh", "pr", "view", str(pr), "--json", "body"])
    if code != 0:
        raise PreflightUnavailable(f"could not read the body of PR #{pr}")
    try:
        return json.loads(out or "{}").get("body") or ""
    except ValueError as exc:
        raise PreflightUnavailable(f"unexpected body payload for PR #{pr}") from exc


def read_commit_messages(base: str, head: str = "HEAD", runner=_default_runner) -> list:
    """The integrated commit messages on `<base>..<head>` via `git log`. RAISES PreflightUnavailable on a git
    failure (never read as no commits). An empty range is a successful read returning []."""
    code, out, _ = runner(["git", "log", "--format=%B%x00", f"{base}..{head}"])
    if code != 0:
        raise PreflightUnavailable(f"could not read commit messages for {base}..{head}")
    return [m for m in (out or "").split("\x00") if m.strip()]


def preflight(pr, base: str, *, runner=_default_runner) -> dict:
    """Run the whole pre-flight for a draft PR at submit: read the will-close set + body + integrated commits,
    then classify. A read failure of ANY input degrades to the could-not-read line (fail-closed), never a false
    clean. Returns the classify() result: {"lines", "defang"}."""
    try:
        body = read_body(pr, runner)
        honored = read_will_close(pr, runner)
        commits = read_commit_messages(base, runner=runner)
    except PreflightUnavailable:
        return classify(body="", honored_local=set(), commit_honored=set(), unavailable=True)
    commit_honored, commit_trap = commit_will_close(commits)
    return classify(body=body, honored_local=honored, commit_honored=commit_honored, commit_trap=commit_trap)


# ---- operator-runnable demo (real classification; only gh/git are faked) ---------------------------

def _fake_runner(*, closing=None, body="", commits=None, fail=None):
    """A stand-in gh/git runner for the demo + tests. `fail` (a command-word like 'gh') forces that binary's
    reads to fail, to exercise the could-not-read degrade. Only the subprocess boundary is faked — the real
    parse/compare/transform logic runs ([[demo-must-exercise-real-logic]])."""
    closing = closing or []
    commits = commits or []

    def runner(cmd):
        binary = cmd[0]
        if fail == binary:
            return 1, "", "forced failure"
        if binary == "gh" and cmd[1:3] == ["pr", "view"]:
            field = cmd[cmd.index("--json") + 1]
            if field == "closingIssuesReferences":
                return 0, json.dumps({"closingIssuesReferences": [{"number": n} for n in closing]}), ""
            if field == "body":
                return 0, json.dumps({"body": body}), ""
        if binary == "git" and cmd[1] == "log":
            return 0, "".join(m + "\x00" for m in commits), ""
        return 1, "", "unhandled"
    return runner


def _demo() -> int:
    print("The close-linkage pre-flight — run at submit, before a draft PR is marked ready. It compares what a\n"
          "PR WILL close (GitHub's own linkage + the integrated commit messages) against what the PR DECLARES,\n"
          "and surfaces any contradiction in the Review record you read at the merge. No real GitHub call is\n"
          "made and no PR is edited.\n")

    scope = "## Scope\n\n**Adds the pre-flight.**\n\n- Part of #274\n\n## Out of scope\n\n- nothing\n"

    # (1) Accidental, body-sourced: a stray `Closes #274` in prose while the scope says only "Part of #274".
    #     Uniquely locatable -> a minimal keyword-only defang, disclosed.
    body1 = scope + "\n## Notes\n\nThis work Closes #274 as it lands the first slice.\n"
    r1 = classify(body=body1, honored_local={274}, commit_honored=set())
    print("1) A stray closing keyword in prose, while the scope says only 'Part of #274':")
    for ln in r1["lines"]:
        print("   • " + ln)
    print(f"   [defang emitted: {'yes' if r1['defang'] else 'no'}; the '#274' reference is kept, only the "
          f"keyword is removed]")
    print()

    # (2) Deliberate close ALONGSIDE Part of -> genuine contradiction -> SURFACE, never defang.
    body2 = "## Scope\n\n- Part of #274\n\n## Out of scope\n\n- x\n\nCloses #274\n"
    r2 = classify(body=body2, honored_local={274}, commit_honored=set())
    print("2) A deliberate 'Closes #274' line alongside 'Part of #274' — ambiguous, so it surfaces (no defang):")
    for ln in r2["lines"]:
        print("   • " + ln)
    print()

    # (3) Comma-trap: `Closes #1, #2` links only #1; #2 stays open.
    body3 = "## Scope\n\n- the work\n\n## Out of scope\n\n- x\n\nCloses #1, #2\n"
    r3 = classify(body=body3, honored_local={1}, commit_honored=set())
    print("3) 'Closes #1, #2' — only #1 closes on merge:")
    for ln in r3["lines"]:
        print("   • " + ln)
    print()

    # (4) Clean: the PR deliberately closes what it says it closes -> NO line.
    body4 = "## Scope\n\n- the whole thing\n\n## Out of scope\n\n- x\n\nCloses #40\n"
    r4 = classify(body=body4, honored_local={40}, commit_honored=set())
    print(f"4) A clean PR (closes exactly what it declares) -> {len(r4['lines'])} line(s) — nothing to surface.")
    print()

    # (5) Could-not-read: the will-close set is unreadable at submit -> the fallback line, never a false clean.
    r5 = preflight(7, "main", runner=_fake_runner(fail="gh"))
    print("5) When the will-close set can't be read at submit:")
    for ln in r5["lines"]:
        print("   • " + ln)
    print()

    # (6) Cross-repo close -> out of reach, surfaced-and-named, never defanged.
    body6 = scope + "\n## Notes\n\nCloses octo/other#9\n"
    r6 = classify(body=body6, honored_local=set(), commit_honored=set())
    print("6) A cross-repo close is out of reach:")
    for ln in r6["lines"]:
        print("   • " + ln)
    print()

    # (7) Commit-sourced accidental close -> SURFACE, never a body defang (a body edit can't neutralize a commit).
    r7 = classify(body=scope, honored_local=set(), commit_honored={274})
    print("7) An accidental close in a COMMIT message (not the body) surfaces — a body edit can't neutralize it:")
    for ln in r7["lines"]:
        print("   • " + ln)
    print(f"   [defang emitted: {'yes' if r7['defang'] else 'no'}]")
    print()

    # (7b) A quoted/fenced comma-trap EXAMPLE (nothing honored) stays silent — no false 'will stay open'.
    body7b = "## Scope\n\n- x\n\n## Out of scope\n\n- y\n\n<!-- the template warns: \"Closes #1, #2\" closes only #1 -->\n"
    r7b = classify(body=body7b, honored_local=set(), commit_honored=set())
    print(f"7b) A quoted 'Closes #1, #2' EXAMPLE GitHub honored nothing of -> {len(r7b['lines'])} line(s) "
          f"(no false warning).")
    print()

    # (7c) A commit-message comma-trap (`Closes #1, #2` in a commit): #1 closes, #2 silently stays open.
    ch, ct = commit_will_close(["feat: land it\n\nCloses #1, #2"])
    r7c = classify(body="## Scope\n\n- x\n\n## Out of scope\n\n- y\n", honored_local=set(),
                   commit_honored=ch, commit_trap=ct)
    print("7c) A comma-trap in a COMMIT message — #2 is named as staying open:")
    for ln in r7c["lines"]:
        print("   • " + ln)
    print()

    # (8) End-to-end through the faked gh/git boundary, exercising the real reads + classify.
    e2e = preflight(7, "main", runner=_fake_runner(closing=[274], body=body1, commits=[]))
    print("8) End-to-end through the (faked) gh/git boundary — the same accidental-body case as (1):")
    for ln in e2e["lines"]:
        print("   • " + ln)
    print()

    # Self-check: each invariant must hold, or the demo fails (a falsification that can fail).
    checks = {
        "an accidental body keyword is defanged, keeping the #274 reference":
            bool(r1["defang"]) and "#274" in r1["defang"]["new_body"]
            and "Closes #274" not in r1["defang"]["new_body"],
        "the defang is byte-identical except the removed keyword":
            r1["defang"]["new_body"] == body1.replace("Closes #274", "#274"),
        "a deliberate close alongside Part-of surfaces, never defangs":
            r2["defang"] is None and any("needs a small edit" in ln for ln in r2["lines"]),
        "the comma-trap leftover (#2) is named as staying open":
            any("#2" in ln and "stay open" in ln for ln in r3["lines"]),
        "a clean PR produces no line": r4["lines"] == [],
        "an unreadable will-close set yields the could-not-read line, not a clean":
            any("will close" in ln for ln in r5["lines"]) and r5["defang"] is None,
        "a cross-repo close is surfaced and never defanged":
            r6["defang"] is None and any("another repository" in ln for ln in r6["lines"]),
        "a commit-sourced accidental close surfaces, never body-defangs":
            r7["defang"] is None and any("needs a small edit" in ln for ln in r7["lines"]),
        "a quoted comma-trap example GitHub honored nothing of raises no line":
            r7b["lines"] == [],
        "a commit-message comma-trap names #2 as staying open":
            any("#2" in ln and "stay open" in ln for ln in r7c["lines"]),
        "end-to-end through the faked boundary defangs the accidental body close":
            bool(e2e["defang"]),
    }
    bad = [name for name, ok in checks.items() if not ok]
    if bad:
        print("DEMO UNEXPECTED — these invariants did not hold:", file=sys.stderr)
        for name in bad:
            print(f"  - {name}", file=sys.stderr)
        return 1
    print("All invariants held: an accidental body keyword is defanged minimally and disclosed; a deliberate\n"
          "close, a commit-sourced close, and a cross-repo close all surface instead; the comma-trap leftover is\n"
          "named; a clean PR is silent; and an unreadable will-close set fails closed to the could-not-read line.")
    return 0


# ---- CLI -------------------------------------------------------------------------------------------

def _parse_args(argv: list) -> dict:
    """`--pr N --base REF [--head REF]` -> {pr, base, head}. Minimal, mirroring the repo's other tool CLIs."""
    args = {"pr": None, "base": None, "head": "HEAD"}
    i = 0
    while i < len(argv):
        if argv[i] in ("--pr", "--base", "--head") and i + 1 < len(argv):
            args[argv[i][2:]] = argv[i + 1]
            i += 2
        else:
            i += 1
    return args


def main(argv: "list | None" = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "demo":
        return _demo()
    if argv and argv[0] == "check":
        args = _parse_args(argv[1:])
        if not args["pr"] or not args["base"]:
            print("usage: close_linkage_preflight.py check --pr N --base REF [--head REF]", file=sys.stderr)
            return 2
        result = preflight(args["pr"], args["base"], runner=_default_runner)
        # Emit a single JSON object: the Review lines, and — when an accidental body close is defang-eligible —
        # the exact defanged body + disclosure the orchestrator applies verbatim (`gh pr edit --body-file`).
        print(json.dumps({"lines": result["lines"], "defang": result["defang"]}))
        return 0
    print("usage: close_linkage_preflight.py [check --pr N --base REF | demo]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
