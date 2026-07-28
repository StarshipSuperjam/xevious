#!/usr/bin/env python3
"""disposition-issue-resolution check (#292) — a `custom/script` CI-suite rule
owned by validators-core.

It confirms a mechanical correlate the merge gate could not check before: that every Issue number a pull
request's `## Review` section cites as a finding disposition ("real but out of scope → logged as #N") resolves
to a real **engine-labeled** GitHub issue (open or closed). The locked finding-disposition routing sends
an out-of-scope finding to a tracked issue; this check *witnesses* that the cited issue is real rather than taking
the pull request's word — binding `hard` on a non-AI correlate (the Issue object the engine cannot fabricate
without filing it).

Two distinct findings, by design, so the two reds carry distinct operator actions:
  - the AIMED bite: a cited #N resolves to nothing (404) or to a non-engine issue → the engine cited a follow-up
    that isn't a real engine-tracked item. ACT (file the issue, or correct the number).
  - the fail-closed verdict (never a false green): the issue API could not be read (403 / >=400 / network /
    timeout / a missing token in CI) → couldn't check. WAIT (it clears on its own).
Pull-request references are skipped (a PR resolves via the issues endpoint but carries a `pull_request` key and is
not a tracked-issue disposition; the `pull_request` check runs BEFORE the label check, since a PR carries labels
too). A green run that resolved at least one cited issue emits a SOFT warrant note stating what the pass does NOT
prove — because validate.py does not render a custom/script rule's own `message`, so the bound is delivered here.

Scope is narrow and disclosed: an *uncited* disposition is unchecked, and *any* real engine-labeled issue
satisfies resolution (existence, not relevance). The check reads only an issue's existence + labels — never its
body. The GITHUB_TOKEN is never echoed to stdout/stderr or into a finding message.

It rides existing machinery: the injectable-transport seam (tests/demo fake ONLY the network and run the real
logic), validate's PR-body reader + `## ` section parser, and the negative-fixture meta-check — its fixture (a
seeded PR body citing a sentinel-nonexistent issue) is witnessed live, so the meta-check job materializes the same
`gh` + `issues: read` runtime this unit needs.
"""
from __future__ import annotations
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (finding, section_blocks, get_pr_body, env_override_path, read)
import github_client  # noqa: E402  (the shared authenticated GitHub API client; request-build)

ENGINE_LABEL = "engine"
_CITATION_RE = re.compile(r"#(\d+)")
_USER_AGENT = "engine-disposition-issue-resolution"


class _Unevaluable(Exception):
    """The issue API could not be read for a cited number (403 / >=400 / network / timeout). Becomes the
    fail-closed verdict — a hard red that says WAIT (it clears on its own), never a silent green."""


class IssueResolver:
    """Resolves a cited issue number to one of {'resolved', 'unresolved', 'skip-pr'}. The transport
    (`transport(method, path) -> (status, json|None)`) is injectable so tests and the demo fake ONLY the network
    and run the real logic. Strictly read-only — it GETs an issue's existence + labels, never its body."""

    def __init__(self, repo: str, token: str, *, transport=None):
        self.repo = repo
        self.token = token
        self._transport = transport or self._http

    def _http(self, method: str, path: str):
        req = github_client.request(path, self.token, user_agent=_USER_AGENT, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:    # surface the status — a 404 is a real answer, not an outage
            return exc.code, None
        except urllib.error.URLError as exc:     # network unreachable -> unevaluable (the WAIT verdict)
            raise _Unevaluable(f"GitHub is unreachable: {exc}") from exc

    def classify(self, number: int) -> str:
        """'resolved' (a real engine-labeled issue, open or closed), 'unresolved' (404, or a non-engine issue),
        or 'skip-pr' (the number is a pull request). Raises _Unevaluable on any read failure that is not a clean
        404."""
        status, data = self._transport("GET", f"/repos/{self.repo}/issues/{number}")
        if status == 404:
            return "unresolved"                  # the cited follow-up does not exist
        if status >= 400 or not isinstance(data, dict):
            raise _Unevaluable(f"GitHub returned {status} for issue #{number}")
        if "pull_request" in data:               # a PR, not a disposition -> skip (BEFORE the label check)
            return "skip-pr"
        labels = [str((lab or {}).get("name", "")).lower()
                  for lab in (data.get("labels") or []) if isinstance(lab, dict)]
        return "resolved" if ENGINE_LABEL in labels else "unresolved"


def cited_issue_numbers(body: str) -> list:
    """The distinct issue numbers cited in the PR body's `## Review` section, in first-seen order."""
    review = validate.section_blocks(body or "").get("Review", "")
    seen, out = set(), []
    for m in _CITATION_RE.finditer(review):
        n = int(m.group(1))
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def findings(tier: str, body: str, resolver: "IssueResolver") -> list:
    """The finding.v1 list for one PR body resolved through `resolver`. Empty (or a single soft warrant) = pass;
    a hard finding = block. Never echoes the token."""
    numbers = cited_issue_numbers(body)
    if not numbers:
        return []
    out, resolved = [], []
    for n in numbers:
        try:
            verdict = resolver.classify(n)
        except _Unevaluable:
            # The WAIT red: we couldn't reach the issue service, so the whole run is unevaluable (fail-closed,
            # never a silent green). Plain, and plainly temporary — phrased for an owner who directs rather than
            # operates (the re-run is an automatic/AI action, not a button to press).
            return [validate.finding(tier,
                    "Couldn't check the follow-up issues cited in this change's Review — the issue service was "
                    "unreachable. This usually clears on its own once the service is back; nothing here is a "
                    "setting that needs changing.")]
        if verdict == "skip-pr":
            continue
        if verdict == "unresolved":
            # Phrased so the resolving condition is described, not commanded — and covers the case where #n was
            # ordinary prose, not a follow-up at all (every #N in the Review section is read as an issue reference).
            out.append(validate.finding(tier,
                       f"The Review section for this change refers to #{n}, but #{n} isn't a real engine-tracked "
                       f"issue — it doesn't exist, or it isn't an engine issue. It clears once that issue is "
                       f"filed, the number is corrected, or — if #{n} wasn't meant as a follow-up issue — the "
                       f"reference is reworded. Every #N in the Review section is read as an issue reference."))
        else:
            resolved.append(n)
    if out:
        return out
    if resolved:
        # The green warrant, delivered as a SOFT note (validate.py does not render a custom/script rule's own
        # message). Existence, not relevance.
        cited = ", ".join(f"#{n}" for n in resolved)
        out.append(validate.finding("soft",
                   f"Checked the follow-up issue(s) cited in this change's Review ({cited}); each is a real "
                   f"engine-tracked item. This confirms they are real — not that every out-of-scope finding was "
                   f"logged, not that a cited issue is the right one for that finding, and not that a disposition "
                   f"was the right call."))
    return out


def emit(fs: list) -> int:
    """Write the finding.v1 array to stdout (the custom/script machine channel) and return 0. Human prose lives
    inside each finding's message, so stdout stays pure JSON — and the token never reaches either stream."""
    print(json.dumps(fs))
    return 0


def _demo() -> int:
    """Falsifiable self-check: fakes ONLY the transport, runs the real findings() logic, and proves the
    fabrication red, the outage red, and the green warrant are distinct and fire on the right inputs."""
    print("DISPOSITION-ISSUE-RESOLUTION DEMO — the follow-up issues the engine cites in Review must be real.\n")
    body = "## Purpose\n\nSeed.\n\n## Review\n\nOne finding was real but out of scope; tracked as #4242.\n"

    class _Fake:
        def __init__(self, behavior):
            self._behavior = behavior

        def classify(self, number):
            if self._behavior == "outage":
                raise _Unevaluable("simulated outage")
            return self._behavior

    act = findings("hard", body, _Fake("unresolved"))
    print(f"A Review citing #4242 when it isn't a real engine issue — ACT red ({len(act)} finding):")
    for f in act:
        print(f"  - [{f['severity']}] {f['message']}")
    wait = findings("hard", body, _Fake("outage"))
    print(f"\nThe issue service unreachable — WAIT red ({len(wait)} finding):")
    for f in wait:
        print(f"  - [{f['severity']}] {f['message']}")
    green = findings("hard", body, _Fake("resolved"))
    print(f"\nA Review citing a real engine issue — green warrant ({len(green)} soft note):")
    for f in green:
        print(f"  - [{f['severity']}] {f['message']}")

    act_ok = bool(act) and act[0]["severity"] == "hard" and "isn't a real engine-tracked issue" in act[0]["message"]
    wait_ok = bool(wait) and wait[0]["severity"] == "hard" and "unreachable" in wait[0]["message"]
    green_ok = (bool(green) and green[0]["severity"] == "soft"
                and "not that a cited issue is the right one" in green[0]["message"])
    if not (act_ok and wait_ok and green_ok):
        print("\nDEMO UNEXPECTED: the act / wait / green split did not behave as expected.", file=sys.stderr)
        return 1
    print("\nDEMO OK — the fabrication red, the outage red, and the green warrant are distinct and correct.")
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    tier = os.environ.get("ENGINE_RULE_TIER", "hard")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    in_ci = bool(os.environ.get("GITHUB_ACTIONS") or os.environ.get("CI"))
    # The fixture seam wins when set (the meta-check's witness run); otherwise read the live event payload.
    seam = validate.env_override_path("ENGINE_DISPOSITION_PR_BODY")
    body = validate.read(seam) if seam else validate.get_pr_body(None)

    if not repo or not token:
        if in_ci:
            # In CI the token IS the gate; a missing one is a real fail-closed condition, never a silent pass.
            return emit([validate.finding(tier,
                         "Couldn't check the follow-up issues cited in this change's Review — no repository "
                         "access token was available in CI. This clears once the run has its token; nothing here "
                         "is a setting that needs changing.")])
        # Locally, fail OPEN with a soft note — the CI run, which has a token, is the real gate.
        return emit([validate.disclosed_noop(
                     "The follow-up issue citations weren't checked here — no repository access token is "
                     "available, which is normal on your own machine. The check that can block a bad citation "
                     "runs in CI.")])
    if body is None:
        return emit([validate.disclosed_noop(
                     "No pull request body was available to read, so the Review section's issue citations "
                     "weren't checked here. The check runs against the pull request in CI.")])
    return emit(findings(tier, body, IssueResolver(repo, token)))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
