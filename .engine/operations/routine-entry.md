---
title: Routine entry — the unattended, scope-locked entry procedure
---

## Purpose

The procedure an unattended Routine session enters when a scheduled automation — a Claude Desktop routine or a
Codex Automation — fires the routine command (`/engine-routine` on Claude Code, `$engine-routine` on Codex).
It is the drift-firewall for unattended work: it confirms the engine's guardrails are actually running, that
the run genuinely cannot ask, and that it is isolated from the operator's checkout; it locks onto the frozen
scope-locked build Issue, advances the build by one planned chunk inside that scope, routes anything needing a
human to a GitHub Issue (because it cannot ask), and never merges the protected branch. Enter it at the start
of every routine fire; the build's actual work follows the distributed-implement workflow in
`.engine/operations/build-orchestration.md`, which this procedure references and does not restate.

## Steps

1. **Confirm the engine's hooks ran (the start-of-session briefing arrived).** An unattended run must not write
   when the engine's local guardrails are off. If this session did not receive its start-of-session briefing —
   or the status readout (`uv run --directory .engine -- python tools/engine_status.py`) shows no recent
   evidence the hooks ran — then on this runtime the write-gate and memory capture are OFF, not merely quiet.
   (On Codex the hooks stay off after any engine update that changed them, until the operator re-trusts them
   with `/hooks`.) Do not enter Routine and do not write: report plainly in the run output that the hooks need
   re-trusting and stop. This is a safety refusal, not an alarm — the operator sees this run in the scheduling
   app's history like any session, so no Issue is filed.
2. **Confirm the run is non-interactive (it genuinely cannot ask).** The operator configured this scheduled
   automation with a non-interactive, auto-approve permission posture — in the Claude Desktop routine's
   permission settings, or the Codex Automation's approval posture — so it proceeds without prompts. This is
   the operator's setup, not something the engine sets here. If that posture is unavailable to the run, report
   it plainly in the run output and stop (again a visible-in-app refusal, no Issue). If an action *mid-build*
   would need an approval this run cannot give, that is a blocker — go to step 7.
3. **Enter the Routine write-stance.** Run
   `uv run --directory .engine -- python tools/modes.py set-routine --session "${CLAUDE_CODE_SESSION_ID}"`
   (the engine resolves the session itself when the flag is absent, as on Codex). `set-routine` grants the
   unattended write-stance ONLY when it can prove this run is in a dedicated worktree, never the operator's
   checkout — the never-strand-main floor, enforced here at entry rather than by prose. If it declines (not an
   isolated worktree, or the session cannot be identified), do not write: report the reason in the run output
   and stop. A visible-in-app safety refusal, not a filed Issue.
4. **Read git state and the frozen scope-locked build Issue, then decide which of three situations holds.**
   The build Issue holds the ordered commit-sequence checklist (the durable plan a cold session reads) and
   the permitted write-scope alongside it.
   - **GitHub is unreachable** — there is no plan to read: exit without proceeding (fail-safe), no Issue.
   - **The build is finished** — every checklist item is already done, or its pull request has been
     finalized or closed. There is nothing to do: exit cleanly (step 7) and **file no Issue — completion is
     never an alarm.** A completed build whose schedule keeps firing simply no-ops each run until the
     operator stops the routine or points it at a new build.
   - **The build cannot be found where one was expected** — the routine is pointed at a build Issue that is
     missing or does not match, *and* git shows no pull request or commits from this routine to resume (a
     genuine first-fire mis-aim, not a finished build). File a durable **misfire Issue** (step 7's helper)
     and exit — but only if an open misfire Issue for this routine is not already present, so a mis-aim
     surfaces **once**, never as a fresh Issue every fire.
5. **On the first fire, echo the build Issue this routine locked onto** — "starting the routine on #N —
   <title>" — so a mis-aimed target surfaces on the first cycle rather than after a wasted batch.
6. **Advance one chunk within the scope-lock.** Find the next unchecked checklist item and its scope;
   execute it so that every write stays inside the permitted write-scope, re-checking scope before each
   commit; add the commit(s) to the open pull request; and report progress derived from git and the
   checklist ("commit X landed — N of M planned done"). Never close or merge the pull request.
7. **Escalate anything that needs a human, because this run cannot ask.** An out-of-scope observation
   files an Issue and the run continues; a genuine blocker or a decision needing a human files an Issue and
   halts this task, leaving a plain-language status that names the next step ("stopped at N of M — I need a
   decision on X; I opened Issue #K. Answer there, then re-run the routine."). Author every such Issue —
   misfire, out-of-scope, or blocker — through the shared engine issue-authoring helper
   (`.engine/tools/issue_author.py`) so it reads like every engine-authored Issue, and **never file one
   that duplicates an Issue already open** for the same thing. A finished build, or a run with no eligible
   scope left, exits cleanly ("nothing to do") and **files no Issue**.

## Done when

The routine either advanced the build by one chunk — commits added inside the scope-lock, progress
reported, the pull request left open — or exited without proceeding. It exits **cleanly and files no
Issue** when the hooks were not running, when the run could not isolate or could not identify its session,
when GitHub is unreachable, or when the build is finished or has no eligible scope left — each a visible-in-app
outcome the operator reads from the run itself, not an alarm. It leaves a durable, operator-visible **Issue**
only for a real mid-build anomaly — a first-fire mis-aim with nothing to resume, an out-of-scope observation,
a blocker, or a decision it cannot make — and **never a duplicate of an Issue already open**. In every outcome
the protected branch was never merged and the operator's checkout was never mutated. An interactive Finalize
session later integrates, reviews for cohesion, validates, and submits the pull request for the operator's
merge.

## Notes

The scope-lock is this run **following the plan**, not a mechanical lock it cannot break — the same honest
tier as the Explore write-gate. What *is* mechanical is the entry: `set-routine` (step 3) refuses the write
stance unless it can prove worktree isolation, so the never-strand-main floor does not rest on prose. The only
unbypassable wall is the protected-branch merge, which Routine never performs; the interactive Finalize review
is the cohesion backstop. Single-flight — skipping a fire while one is already in progress — is the scheduler's
behavior where it provides it (the Claude Desktop routine does; whether a Codex Automation does is not verified
from inside the engine), so two overlapping fires are possible and are bounded only by the no-merge wall and
the Finalize review, not by a lease; orphan recovery is reading git state — a run that dies mid-task leaves its
commits (or none) and the PR open, and the next run resumes from git and the checklist. The non-interactive
posture and the worktree isolation are operator-side settings in the scheduling app, set during setup; this
procedure confirms them (step 1's hooks check, step 3's mechanical isolation gate) but never sets them.
