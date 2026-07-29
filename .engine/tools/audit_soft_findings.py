#!/usr/bin/env python3
"""Render the currently-firing SOFT validator findings as a feed for the read-only audit
persona (issue #273 half 2).

The gap this closes: a SOFT validator finding (a non-blocking nudge — a runbook over its line
budget, say) is printed to a CI log and discarded. Nothing durable holds it, so the weekly
self-review never sees it and a recurring nudge drifts forever, dismissed each session as
"pre-existing". This feed hands the audit the soft findings firing right now, so a genuine
standing one can be judged and surfaced as tracked work.

How it stays honest and safe:
- It reads the report-only `audit-prep` suite through `validate.collect` — the data seam, never
  scraped human stdout — so the feed can't drift when the report format changes.
- It keeps only SOFT-severity findings. A hard structural finding is a different problem the CI
  gate already blocks on; it is not a standing "trim me" nudge and does not belong in this feed.
- It DEFANGS every author-controllable string (the finding message and its file path embed
  author-chosen filenames) with the same prompt-fence neutraliser every other persona feed uses —
  this text is dropped between `BEGIN/END` markers in the persona's prompt, so a filename forging
  a closing marker must not be able to break out.
- It distinguishes three states plainly: findings present; ran clean (nothing firing — a real
  all-clear, not a gap); and could-not-run (a config error — an honest marker so the persona
  discloses the gap instead of reading an empty feed as "all clear")."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import validate  # noqa: E402

# The report-only suite the standing soft-nudge rules join. Reading it (never CI) keeps the feed
# to advisory signals and never re-runs the blocking gate.
FEED_SUITE = "audit-prep"


def render() -> str:
    """The persona-ready feed string for the current soft findings (one of the three states)."""
    try:
        findings = validate.collect(FEED_SUITE, {})
    except Exception as exc:  # noqa: BLE001 — a config error (undeclared suite / unloadable rules)
        # Could-not-run: an honest in-band marker, never a silent empty. A clean exit on a
        # report-only suite does NOT prove the suite evaluated, so the feed must say plainly when
        # it could not be read rather than let the persona read silence as "nothing is firing".
        return ("CURRENTLY-FIRING SOFT FINDINGS: could not be read this run "
                f"({exc}). Treat this concern as not reviewed and say so plainly in your digest; "
                "do not read this as 'nothing is firing'.")
    # Defensive: a disclosed no-op (a check reporting "not applicable here, nothing to do") is by
    # definition never a standing nudge to trim, so it must never reach this feed. None of the current
    # `audit-prep` rules emit one, so this filters nothing today — it keeps the feed honest if such a
    # rule is ever added to the suite later.
    soft = [f for f in findings if f.get("severity") != "hard" and not f.get("not_applicable")]
    if not soft:
        return ("No standing soft validator findings are firing this run — every catalogued "
                "surface is within its budget and shape. This is a clean read, not a gap.")
    out = ["These non-blocking soft findings are firing right now (each is a nudge, never a merge "
           "block). Judge which is a genuine signal worth surfacing as tracked work and which is a "
           "benign disclosure (whether one is genuinely recurring is corroborated from your prior "
           "digests, not from this single snapshot):"]
    for f in soft:
        msg = validate.defang_prompt_fence_markers(f.get("message", ""))
        loc = f.get("location") or {}
        where = validate.defang_prompt_fence_markers(loc.get("file") or "")
        out.append(f"  - {msg}" + (f" [{where}]" if where else ""))
    return "\n".join(out)


def main(argv: list) -> int:
    print(render())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
