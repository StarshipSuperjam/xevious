#!/usr/bin/env python3
"""Passive pre-arm for a merge blocked by an unresolved review conversation — the branch ruleset's
conversation-resolution rule (engine-template #408).

The branch ruleset requires every review conversation to be resolved before merging. When one is still open,
GitHub simply GREYS the merge button — a native state a non-engineer cannot self-diagnose (all the checks are
green, yet the button won't press). This renders a short plain-language notice the orchestrator folds into the
pull request's **Review** record, so the explanation is already in front of the operator BEFORE they hit the
greyed button: why it's blocked, that they may resolve it themselves once they've read and accepted the comment
(never a bare "one click fixes it"), and how to reach the comment — including one hidden as "outdated" after a
rebase.

**Passive pre-arm only — the engine never acts.** It does NOT fetch the live review threads, does not name the
specific open comment, and NEVER auto-resolves a thread (auto-resolving a comment that flagged a concern would
gut the finding-disposition trust spine the rule serves — the engine declines that active duty). A non-engineer who
still cannot locate the control after reading this is an accepted v1 residual, named honestly rather than closed.

**Not a check, not a gate.** There is no `.engine/check/*.json`, no CI suite entry, no merge gate. Its delivery
rides the AI-authored Review record, so it inherits Review's posture-truthfulness tier — the same shape
`spec_referent.py review-steps` and `close_linkage_preflight.py` already use (a tool whose output the orchestrator
drops into Review verbatim).

**Standing copy, collapsed by default.** The text is identical on every pull request (no per-PR data), so it is
rendered as a one-line summary with a `<details>` expansion — present in advance without drowning the per-PR
Review content on the ~99% of merges where nothing is greyed (the engine's collapse-not-suppress anti-habituation
posture). The wording is a tested constant bound by the plain-language leak-guard, not
prose re-authored each render, so it cannot drift below the bar that makes the operator's consent informed.

Usage:
  uv run --directory .engine -- python tools/unresolved_conversation_notice.py         # print the Review block
  uv run --directory .engine -- python tools/unresolved_conversation_notice.py demo    # self-check the copy
"""
from __future__ import annotations

import sys

# The one-line summary the operator always sees (the `<details>` handle). Plain, non-alarming, and never a bare
# "one click fixes it" — it says a greyed button MAY be a conversation, and points at what to do, not at the click.
_SUMMARY = ("If the merge button is greyed but every check passed, it may be an unresolved review conversation "
            "— here's what that means and what you can do.")

# The three things the locked spec requires this convey (why blocked / may-clear-after-reading-and-accepting /
# how to reach it including the post-rebase-hidden case). Peer voice, no engine or GitHub jargon beyond the
# operator-visible button and tab names they'll actually look for.
_BODY = (
    "GitHub won't let a pull request merge while a review comment on it is still marked unresolved — even when "
    "all the automated checks are green. The merge button just greys out. This isn't a failure; it's a comment "
    "someone left that hasn't been settled yet.\n\n"
    "You can settle it yourself. Read the comment, decide you're genuinely satisfied it's been handled, and then "
    "mark it resolved — resolving it is you accepting the point it raised, so it's worth doing only once you've "
    "read it, never as a formality to get past the button. The engine never resolves one of these for you: a "
    "comment flagged a concern, and clearing it unread would defeat the reason it's there.\n\n"
    "To find it: open the pull request's **Conversation** tab (or **Files changed**) and look for the comment "
    "with a **Resolve conversation** button. If a rebase or a force-push moved the lines the comment was attached "
    "to, GitHub marks it **outdated** and hides it — on the **Conversation** tab, expand the collapsed "
    "**outdated** comments to reach it, read it there, and only then resolve it."
)


def render() -> str:
    """The plain-language Review-section block the orchestrator drops in verbatim: a one-line summary with a
    `<details>` expansion carrying the full explanation. Deterministic, so it is testable."""
    return f"<details>\n<summary>{_SUMMARY}</summary>\n\n{_BODY}\n</details>"


def _demo() -> int:
    """Self-check: the rendered block carries all three required things in plain language, keeps the
    read-then-accept binding even for the post-rebase-hidden case, and never degrades to a bare 'one click'."""
    block = render()
    checks = {
        "why it's blocked (greyed / unresolved)": "unresolved" in block and "grey" in block.lower(),
        "may clear after reading + accepting": "read" in block.lower() and "accepting" in block.lower(),
        "how to reach it": "Resolve conversation" in block and "Conversation" in block,
        "post-rebase / outdated case": "outdated" in block and ("rebase" in block or "force-push" in block),
        "read-then-accept kept, no bare 'one click'": "one click" not in block.lower()
        and "only then resolve" in block.lower(),
        "collapsed (details/summary)": "<details>" in block and "<summary>" in block,
    }
    ok = all(checks.values())
    for label, passed in checks.items():
        print(f"  [{'ok' if passed else 'XX'}] {label}")
    print("unresolved-conversation notice self-check:", "OK" if ok else "FAILED")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    print(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
