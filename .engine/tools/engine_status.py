#!/usr/bin/env python3
"""`/engine-status` — the operator's on-demand view of where the project stands (issue #83).

The PULL half of the operator-presentation relay. Boot PUSHES the safety-critical briefing
every session (the alarms + the present-marker the AI must relay); this verb PULLS the routine status
dashboard on demand — milestone, what's next, what recently shipped, what needs attention. It is
`operator-typed`: the operator types `/engine-status` to see it. The assistant does not invoke the skill,
but still surfaces this status when the operator asks where things stand — by running this tool directly,
the cue for which lives in the boot pack. Read-only: it changes nothing.

It is a thin reuse of boot's seam — `gather_signals` (boot's SOLE I/O boundary) then `render_dashboard`
(boot's PURE, operator-toned renderer). This is the design's "two renderings of the same data": boot wraps
that dashboard in an AI-facing briefing; this surfaces the SAME dashboard directly to the operator. boot is
a lifecycle tool (not a guard tool), so importing it to reuse the shared renderer is the intended structure,
not a layering breach — and it keeps the one renderer in one place so the two views can never drift.
"""
from __future__ import annotations
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import boot  # noqa: E402  (the lifecycle tool that owns the signals seam + the shared pure renderer)
import modes  # noqa: E402  (the session-id resolver the operator-typed verbs share)

# Operator-facing strings this tool adds (the dashboard body is boot's, vetted there). Kept as constants so
# they read in plain language and a test can check them at the source — no leaked engine/maintainer jargon.
_DEGRADED = "I couldn't put the full status together just now. Please try again in a moment."
_DEMO_INTRO = "What /engine-status shows you — where your project stands right now:"
_DEMO_EXAMPLE_BANNER = "─── EXAMPLE — a made-up situation, NOT your project ───"
_DEMO_EXAMPLE_INTRO = "And here is what the view looks like when something needs your attention:"


def render(session_id: str | None = None) -> str:
    """The operator-facing status dashboard: gather the signals (boot's sole I/O boundary), then render the
    pure operator-toned body. Always answers — if assembling it raises, degrade to a plain line rather than
    blanking or erroring (the same always-answers posture as `/engine-help` and boot's own pack guard)."""
    try:
        return boot.render_dashboard(boot.gather_signals(session_id))
    except Exception:
        return f"## {boot.PRESENT_MARKER}\n{_DEGRADED}"


# A made-up signals set for the demo's "what an alarm looks like" example. It carries the keys render_dashboard
# reads by hard subscript (a missing one would raise) plus the optional folder-health / recovery signals it reads
# defensively, and is deliberately a gate-off situation so the demo shows the loudest alarm. Pure data — no I/O.
_EXAMPLE_SIGNALS = {
    "state": {"schema_version": 1,
              "standing_situation": {"milestone": ["Ship the beta"], "phase": "Building the checkout page"},
              "integration_debt": {}},
    "refused": False,
    "gate": "off",
    "reason": "branch protection not detected",
    "finding_count": 1,
    "register": "https://github.com/your-org/your-project/issues?q=is:open+label:engine",
    "debt_count": 0,
    "debt_as_of": None,
    "att_lines": ["Turn branch protection back on so unreviewed changes can't reach your main branch."],
    "att_degraded": [],
    "shipped": ["#42 Add the sign-in page", "#41 Set up the database"],
    "stance": "Looking around — reading and planning, not changing anything yet.",
    "strand": None,   # the operator-checkout strand signal; None = the folder is healthy
    "behind_origin": None,   # the behind-the-main-line tail (#335/#342); None = the folder isn't missing merged work
    "off_main": None,   # the off-main Stage-1 signal (#342); None = the folder is on its main line of work
    "pr_conflict": None,   # the stranded-PR conflict signal (#136); None = no pull request is stuck
    "restore_offer": None,   # the memory auto-restore offer; None = memory present or no backup configured
    # A representative self-review-has-gone-stale finding so the example also shows the
    # gentle freshness advisory in the attention list. Illustrative wording — the real text comes from
    # audit_digest.staleness(); render_dashboard reads only its severity + message.
    "audit_stale": {"severity": "soft",
                    "message": "The engine hasn't reviewed its own health in a while — re-arm the scheduled "
                               "self-review so it refreshes on the next run, or ask me to do it for you."},
    # the live-derived "where we are" (boot #100); present here so the example shows the current live line
    "live_standing": {"milestone": ["Ship the beta"], "phase": "Building the checkout page (issue #128)"},
}


def _demo() -> int:
    """An operator-runnable demonstration of `/engine-status`. Prints the real status for THIS project, then
    a clearly-labelled made-up example of what the view looks like when something needs attention — so the
    operator can see both the all-clear shape and an alarm shape with their own eyes, without a real alarm
    having to fire. The example is pure data, so it renders the same every time."""
    print(_DEMO_INTRO + "\n")
    live = render()
    print(live)
    print()
    print(_DEMO_EXAMPLE_BANNER)
    print(_DEMO_EXAMPLE_INTRO + "\n")
    example = boot.render_dashboard(_EXAMPLE_SIGNALS)
    print(example)
    # Self-check: the live status rendered, and the example alarm shape rendered its distinctive attention
    # line (so the operator sees both an all-clear and an alarm shape with their own eyes).
    ok = bool(live) and "branch protection" in example
    if not ok:
        print("\nDEMO UNEXPECTED: the example alarm dashboard did not render its expected attention line.",
              file=sys.stderr)
        return 1
    return 0


def main(argv: list) -> int:
    if argv and argv[0] == "demo":
        return _demo()
    # The Claude skill passes `--session "${CLAUDE_CODE_SESSION_ID}"` (the shell expands that env var);
    # if the argument arrives empty or unexpanded, _resolve_session falls through to the provider seam's
    # resolution chain (providers.resolve_session — the env chain, then the Codex live-session marker),
    # so the status reflects the REAL session's stance (looking-around vs building) rather than a default.
    print(render(modes._resolve_session(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
