#!/usr/bin/env python3
"""Derive the project's standing-situation ("where we are") LIVE from native GitHub sources (engine-template
#100).

The corrected design: "where we are" is a **read-only projection of native sources**, assembled live at
orientation — never a stored marker any session *advances* (that was the rejected category error). This module
is that projection:

  - **phase** <- the most-recently-**merged pull request**, formatted "<title> (PR #N)" — the honest "what
    merged last", read directly from the merge record rather than inferred. Take the recent closed PRs, keep the
    merged ones, order them by `merged_at` (newest first), and take that PR's title. This reads the actual last
    merge (any PR, whatever it closed), so it never falls through to an older item because a PR listed a
    non-engine issue first — the failure the earlier closing-ref/engine-label walk was prone to. The persisted
    cache key stays `phase` (schema/cache continuity); its meaning is now "the last merged PR", not a plan cursor.
  - **milestone** <- the titles of the project's OPEN GitHub Milestones, read as they are — every open one, in
    GitHub's earliest-due-first order — or an empty list when the project keeps none ("none set" — the honest
    normal state, not an error). GitHub has no notion of a single "current" milestone, so the engine names what
    is open and elects none of them (engine-template #496).

This is the strong/best-effort split the design names: `phase` is engine-derivable (strong, like the debt
count); `milestone` is operator-plan-derivable (best-effort — None when no Milestone exists).

**All-or-nothing degradation.** A *read failure* (any HTTP >= 400, an unreachable host, an unexpected response
shape) is NEVER swallowed as "nothing here" — it RAISES `DeriveUnavailable`, so [boot] falls back to the
committed offline cache (rendered stale-labelled) rather than presenting a confident, wrong live answer. A *successful* read that simply finds nothing returns None for that field,
which boot renders as the honest live "none set" / "—".

This module is a **pure leaf**: it imports only the standard library and takes an injected GitHub reader
(`gh`, duck-typed: `gh.repo`, `gh.label`, `gh._transport(method, path, body) -> (status, json)` — the seam
[telemetry].GitHubIssues exposes). It imports neither boot nor telemetry, so there is no import cycle: boot
calls it for the live display, telemetry calls it to refresh the offline cache on its GitHub pass. It performs
**no writes** — it never touches state.json, never advances a marker, never hand-types a value.

Run the demo: `uv run --directory .engine -- python tools/standing_situation.py demo`
"""
from __future__ import annotations
import sys

# How many recent closed PRs to scan for the latest merged one before giving up (returning None for phase). A
# window because the newest CLOSED PRs may be unmerged (closed without merging); we want the newest MERGED one.
_PR_WINDOW = 30


class DeriveUnavailable(Exception):
    """Raised when GitHub cannot be read while deriving the standing-situation (an outage, or a 4xx/5xx
    auth/scope error, or an unexpected response shape). It is NEVER swallowed as "no milestone / no phase":
    a read failure that read as genuine-absence would silently present a confident, wrong live card — the
    exact #100-class trust failure this correction exists to remove. Boot catches it and falls back to the
    committed offline cache, rendered stale-labelled."""


def _read(gh, path: str):
    """One read through the injected transport. Raises DeriveUnavailable on any failure — an HTTP error
    status (>= 400) or a null body — so a read failure can never be mistaken for genuine absence."""
    status, data = gh._transport("GET", path, None)
    if status >= 400 or data is None:
        raise DeriveUnavailable(f"GitHub returned {status} reading {path}")
    return data


def derive_milestone(gh) -> list[str]:
    """The titles of the project's OPEN Milestones, read as they are — every open one, in GitHub's
    earliest-due-first order — or an empty list when there are none ("none set", the honest normal state, not
    an error). GitHub has no notion of a single "current" milestone, so the engine names what is open and
    elects none of them (engine-template #496); a read failure raises DeriveUnavailable (never read as an
    empty "none set")."""
    data = _read(gh, f"/repos/{gh.repo}/milestones?state=open&sort=due_on&direction=asc&per_page=100")
    if not isinstance(data, list):
        raise DeriveUnavailable("milestones response was not a list")
    # Every open milestone named (blank titles dropped), electing none — not just data[0], the pre-#496 pick.
    return [title for m in data if isinstance(m, dict) and (title := (m.get("title") or "").strip())]


def derive_last_merged(gh, *, window: int = _PR_WINDOW) -> str | None:
    """The most-recently-merged pull request, as "<title> (PR #N)", or None when the recent window holds no
    merged PR. Reads the recent closed PRs, keeps the merged ones, orders them by `merged_at` (newest first),
    and returns that PR's title — the actual last merge, whatever it closed. A read failure raises
    DeriveUnavailable (never read as "nothing merged"). It reads the merge record directly — no body parsing,
    no second read, no engine-label filter — so it never falls through to an older item the way the old
    closing-ref walk could when a PR listed a non-engine issue first."""
    pulls = _read(gh, f"/repos/{gh.repo}/pulls?state=closed&sort=updated&direction=desc&per_page={window}")
    if not isinstance(pulls, list):
        raise DeriveUnavailable("pulls response was not a list")
    # The REST API has no `sort=merged`, and a post-merge edit can bump `updated`; so among the closed PRs we
    # keep only the merged ones and order them by `merged_at` ourselves — truly "most-recently-merged" first.
    merged = sorted((pr for pr in pulls if isinstance(pr, dict) and pr.get("merged_at")),
                    key=lambda pr: pr["merged_at"], reverse=True)
    for pr in merged:
        number = pr.get("number")
        title = (pr.get("title") or "").strip()
        if title and isinstance(number, int):   # skip a blank-titled PR rather than render "… (PR #N)" nameless
            return f"{title} (PR #{number})"
    return None                                  # no merged PR in the window -> phase is "—" (honest)


def derive_standing_situation(gh) -> dict:
    """Assemble {"milestone", "phase"} live from native sources, read-only. ALL-OR-NOTHING: if either read
    fails, DeriveUnavailable propagates (boot then shows the cached line). On success, milestone is the list of
    open Milestone titles (empty when none) and phase ("what merged last") is None when the window holds no
    merged PR (boot renders the honest "none set" / "—"). Milestone is read first so a read failure
    short-circuits before the last-merged read."""
    milestone = derive_milestone(gh)
    phase = derive_last_merged(gh)
    return {"milestone": milestone, "phase": phase}


# ---- operator-runnable demo (real derive logic; only the GitHub network is faked) -----------------

class _FakeGH:
    """A stand-in GitHub reader for the demo: a fixed repo/label and an injected transport. Lets the demo run
    the REAL derive + render logic fully offline ([[demo-must-exercise-real-logic]]) — only the network is faked."""

    def __init__(self, transport, *, repo="your-org/your-project", label="engine"):
        self.repo = repo
        self.label = label
        self._transport = transport


def _canned(milestones, pulls):
    """Build a transport answering the two GETs the derive makes, from canned fixtures. `milestones` is the
    open-milestones payload; `pulls` the recent closed-PRs payload (each {number, title, merged_at})."""
    def transport(method, path, body):
        if "/milestones" in path:
            return 200, milestones
        if "/pulls" in path:
            return 200, pulls
        return 404, None
    return transport


def _fail_transport(method, path, body):
    """A transport that always reports a read failure (here, an auth error), to demonstrate the all-or-nothing
    fall-back to the cached line — a failure must NEVER read as a confident live 'none set'."""
    return 403, None


def _where_lines(boot, *, live, state) -> list:
    """Render the REAL boot card over a complete signals dict and return its standing block — the
    'What merged last' line, the 'Milestone' line, and the cached-staleness sub-line when present — so the
    operator sees the actual card text, not a Python structure. The count/total signals boot's renderer
    reads via `.get()` are deliberately absent here — this demo isolates the standing block."""
    signals = {"state": state, "refused": False, "gate": "on", "reason": None,
               "finding_count": 0, "register": "",
               "debt_count": 0, "debt_as_of": None, "att_lines": [], "att_degraded": [],
               "shipped": [], "stance": "Exploring", "strand": None, "pr_conflict": None,
               "restore_offer": None, "audit_stale": None, "live_standing": live}
    lines = boot.render_dashboard(signals).splitlines()
    out = []
    for i, ln in enumerate(lines):
        if ln.startswith("**What merged last"):
            out.append(ln)
            for nxt in lines[i + 1:i + 3]:          # the Milestone line + an optional staleness sub-line
                if nxt.startswith("**Milestone") or nxt.startswith("_("):
                    out.append(nxt)
                else:
                    break
            break
    return out


def _demo() -> int:
    import boot  # lazy (boot imports this module at top; importing it here avoids a load-time cycle)

    print("What boot shows for 'what merged last' — derived live from your GitHub each session:\n")

    # (1) SEVERAL open milestones — GitHub elects none, so the engine names them all — plus two merged PRs, the
    #     newest of which is what shows. #42 (newer merged_at) must win over #41.
    gh1 = _FakeGH(_canned(
        milestones=[{"title": "Ship the beta", "due_on": "2026-09-01T00:00:00Z"},
                    {"title": "Public launch", "due_on": "2026-11-01T00:00:00Z"}],
        pulls=[{"number": 41, "title": "Wire up the cart", "merged_at": "2026-06-08T00:00:00Z"},
               {"number": 42, "title": "Add the checkout page", "merged_at": "2026-06-10T00:00:00Z"}]))
    print("1) Several open milestones — the engine names them all, electing none — and the last merged PR:")
    l1 = _where_lines(boot, live=derive_standing_situation(gh1), state=None)
    for ln in l1:
        print("   " + ln)
    print()

    # (2) A project that keeps NO milestone (this repo's real state) — "No milestone is open" is honest-normal.
    gh2 = _FakeGH(_canned(
        milestones=[],
        pulls=[{"number": 99, "title": "Stop the operator checkout drifting", "merged_at": "2026-06-13T00:00:00Z"}]))
    print('2) A project that keeps NO GitHub milestone — "No milestone is open" is a normal state, not an error:')
    l2 = _where_lines(boot, live=derive_standing_situation(gh2), state=None)
    for ln in l2:
        print("   " + ln)
    print()

    # (3) GitHub unreachable / auth failed -> the derive RAISES, so boot shows the cached copy, stale-labelled.
    #     A read failure must NEVER read as a confident live 'none set'.
    try:
        live3 = derive_standing_situation(_FakeGH(_fail_transport))
    except DeriveUnavailable:
        live3 = None
    cached_state = {"schema_version": 1,
                    "standing_situation": {"milestone": None, "phase": "Add the checkout page (PR #42)",
                                           "as_of": "2026-06-10T12:00:00Z"},
                    "integration_debt": {}}
    print("3) When GitHub can't be read, boot falls back to the last cached copy and says so plainly:")
    for ln in _where_lines(boot, live=live3, state=cached_state):
        print("   " + ln)
    print()

    # (4) The stale-fallthrough fix (defect A): the NEWEST merged PR here closes a non-engine issue first. The
    #     old closing-ref/engine-label walk skipped such a PR and fell through to an OLDER engine issue, showing
    #     a stale item as "where we are"; the new derive reads the merge record directly, so the newest wins.
    gh4 = _FakeGH(_canned(
        milestones=[],
        pulls=[{"number": 111, "title": "Fix the submit base conflation", "merged_at": "2026-07-19T01:00:00Z"},
               {"number": 90, "title": "Name every open milestone", "merged_at": "2026-07-18T22:00:00Z"}]))
    print("4) The stale-fallthrough fix (defect A) — the newest merged PR always wins:")
    print("   BEFORE — the closing-ref walk could skip the newest PR and show an OLDER item as current.")
    print("   AFTER  — derived from the merge record, so the actual last merge shows:")
    l4 = _where_lines(boot, live=derive_standing_situation(gh4), state=None)
    for ln in l4:
        print("            " + ln)
    print()

    # (5) MANY open milestones (#558): past a glanceable few the line names the first five and moves the true
    #     total into the engine's own label — a disclosed sample, electing none. Seven open here, cap five.
    gh5 = _FakeGH(_canned(
        milestones=[{"title": f"Phase {i}", "due_on": f"2026-0{i}-01T00:00:00Z"} for i in range(1, 8)],
        pulls=[{"number": 51, "title": "Wire the thing", "merged_at": "2026-06-14T00:00:00Z"}]))
    print("5) Many open milestones — the first five are named and the true total is disclosed, electing none:")
    l5 = _where_lines(boot, live=derive_standing_situation(gh5), state=None)
    for ln in l5:
        print("   " + ln)
    print()

    print("Note: this walkthrough uses a maintainer-style pull request title so the raw shape is visible; in a")
    print("project, PR titles read cleanly for you. No real GitHub call was made, and your saved status was not")
    print("modified.")
    # Self-check (must be able to FAIL on a broken derivation — [[demo-must-exercise-real-logic]]): scenario 1
    # names BOTH open milestones under the plural label AND shows the NEWEST merged PR (#42, not the older #41);
    # scenario 2 names no milestone; scenario 4 shows the newest PR (#111, not the older #90) — the defect-A
    # guarantee; scenario 5 caps at five names and discloses the true total of seven, naming none beyond the cap
    # (#558); an unreadable GitHub raises (falls back to the cached copy), never a confident live 'none set'.
    ok = (all(any(name in ln for ln in l1) for name in ("Ship the beta", "Public launch"))
          and any("Milestones:" in ln for ln in l1)
          and any("Add the checkout page (PR #42)" in ln for ln in l1)
          and not any("Ship the beta" in ln for ln in l2)
          and any("Fix the submit base conflation (PR #111)" in ln for ln in l4)
          and any("(showing 5 of 7 open)" in ln for ln in l5)
          and any('"Phase 1"' in ln for ln in l5)
          and not any('"Phase 7"' in ln for ln in l5)
          and live3 is None)
    if not ok:
        print("\nDEMO UNEXPECTED: the live 'what merged last' derivation did not behave as expected across the "
              "milestone / no-milestone / newest-PR-wins / unreadable cases.", file=sys.stderr)
        return 1
    return 0


def main(argv: list | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv[1:])
    if argv and argv[0] == "demo":
        return _demo()
    print("usage: standing_situation.py demo")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
