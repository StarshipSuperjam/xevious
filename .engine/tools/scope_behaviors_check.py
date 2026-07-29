#!/usr/bin/env python3
"""Soft check: nudge that a pull request declares its Behaviors — the falsifiable capabilities it
delivers, each naming the test or demo that exercises it — as a `### Behaviors` subsection under Scope.

A custom/script (not the `presence` kind) because the declaration is a level-3 subsection, kept there
deliberately so it does NOT add a ninth level-2 heading — the committed template's level-2 headings are
locked to the eight canonical sections the hard completeness check enforces (test_seed.py). This is a
SOFT nudge: it never blocks a merge, and a change with no observable behaviour (a dependency bump, a
docs-only edit, a pure refactor) can leave the section saying so.

Emits a finding.v1 JSON array on stdout — the custom/script contract. Reads the PR body from the trusted
event context ($GITHUB_EVENT_PATH); with no body available (a local rehearsal, no event) OR an unreadable
event, it emits a disclosed soft no-op rather than a false nudge — so this soft check can NEVER become a
hard fail-closed block on CI-infra trouble."""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402  (reuse the trusted body reader + the placeholder-aware emptiness test)

TIER = os.environ.get("ENGINE_RULE_TIER", "soft")
_HEADING = re.compile(r"^###[ \t]+Behaviors[ \t]*$", re.MULTILINE)
_FENCE = re.compile(r"^[ \t]*(```|~~~)")

NUDGE = (
    "The pull request should declare its Behaviors — a `### Behaviors` subsection under Scope listing "
    "the falsifiable capabilities this change delivers, each naming the test or demo that exercises it — "
    "so the change is weighed by the behaviour it completes, not its line count. This is a SOFT nudge: it "
    "never blocks a merge. A change with no observable behaviour (a dependency bump, a docs-only edit, a "
    "pure refactor) can leave the section saying so. Whether a declared behaviour is genuinely WHOLE — "
    "not a contrived fragment — is the reviewer's judgement, not this check's; it only surfaces that the "
    "declaration was made."
)


def _strip_fences(body: str) -> str:
    """Drop fenced code blocks so a `### Behaviors` line inside ``` … ``` (or ~~~) is not mistaken for a
    real subsection heading — matching the codebase's fence-aware convention (validate.section_blocks)."""
    out, fence = [], None
    for line in body.splitlines():
        marker = _FENCE.match(line)
        if fence is None and marker:
            fence = marker.group(1)
            continue
        if fence is not None:
            if line.lstrip().startswith(fence):
                fence = None
            continue
        out.append(line)
    return "\n".join(out)


def _behaviors_block(body: str) -> "str | None":
    """The text of the `### Behaviors` subsection, or None if the subsection is absent. Runs to the next
    level-1/2/3 heading (so it stops at the sibling `## Out of scope` or any later `###`). Fence-aware."""
    body = _strip_fences(body)
    match = _HEADING.search(body)
    if not match:
        return None
    rest = body[match.end():]
    nxt = re.search(r"^#{1,3}[ \t]+", rest, re.MULTILINE)
    return rest[:nxt.start()] if nxt else rest


def findings() -> list:
    try:
        body = validate.get_pr_body(None)
    except Exception:
        # An unreadable/malformed event must NOT crash this script — a non-zero exit would be turned into
        # a HARD fail-closed block by the custom/script runner, breaking the "never blocks" guarantee.
        return [{"severity": "soft", "not_applicable": True,
                 "message": "Could not read the pull-request body (unreadable event context); the "
                            "Behaviors nudge was not evaluated. Soft — it never blocks a merge."}]
    if body is None:
        return [{"severity": "soft", "not_applicable": True,
                 "message": "PR body not available (no event context); the Behaviors nudge was not "
                            "evaluated. In CI the body is present and the nudge runs."}]
    block = _behaviors_block(body)
    if block is None or validate.is_empty_section(block):
        return [{"severity": TIER, "location": None, "message": NUDGE}]
    return []


def main() -> int:
    print(json.dumps(findings()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
