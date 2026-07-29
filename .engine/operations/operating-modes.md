---
title: Operating modes — the session stance and the Explore write-gate
---

## Purpose

Keep a session honest about what it may do. Every session runs in one of three stances — **explore**
(the default), **build**, or **routine** — and this runbook is the operating guide to that stance: how
exploring gates the building actions, how a session deliberately enters build, and why the gate is a
deliberate-effort nudge rather than a wall. Enter it whenever you need to understand or explain why a
building action was refused while exploring, or what changes when a session starts building.

## Steps

The mechanism is `.engine/tools/modes.py` — the ephemeral, session-keyed stance signal, the `PreToolUse`
write-gate, and the `PostToolUse` plan-acceptance Build-entry trigger, wired as hooks in
`.claude/settings.json`. The stance lifecycle:

1. **Every session boots in explore.** At session start, boot clears the stance signal first
   (`modes.clear_stance`), so a resumed session does not inherit a prior build stance. When the signal is
   absent, unreadable, or unrecognized, the stance is explore — the safe default is the floor, never the
   ceiling. (Any absent, unreadable, or unrecognized signal reliably resolves to explore; the boot clear
   that removes a prior marker is best-effort, and the protected-branch merge is the absolute backstop.)
2. **While exploring, the gate denies the building actions and allows everything else.** It denies the
   small enumerated set that begins building — editing files (Edit / Write / MultiEdit / NotebookEdit),
   creating a branch, committing, and opening a pull request (via `gh pr create` or the GitHub MCP
   create-pull-request tool) — with a plain sentence that names what was blocked and the way forward. That
   denial rides the platform's `PreToolUse` reason channel, which does not reliably reach the operator's
   screen, so the assistant relays it to the operator in plain words — never a silent refusal. One denial
   carries a memory-specific relay: a hand-edit of a memory store (the engine's own `.engine/memory/` or
   the harness auto-memory notebook) is most often the operator asking to be remembered, so instead of the
   build-set "open a pull request" line it confirms a competent "noted" and that the engine records it
   automatically — readable back on request — never a code-change refusal (#257). It
   allows reading, running read-only commands and tests, greps, spawning subagents, and logging issues.
   It also allows Claude Code's own plan file — that is planning, not building — recognized by the
   platform's plan-mode marker, not a path, so it holds even if the plan folder is moved into the repo; a
   write to the operator's own `~/.claude/` config carries no such marker and stays denied. An action it
   cannot classify is allowed: there is no default-deny, because exploring must stay the comfortable place
   to work.
3. **To start building, the operator either types `/engine-start` or accepts a plan.** `/engine-start` is
   an operator-only command the model cannot invoke itself (it carries the platform's operator-only flag,
   and the skill-coherence check holds that flag in place); accepting a plan the model proposed also enters
   build (the model cannot accept its own plan). Either way the stance flips to build and the gate permits
   the writes. On plan-acceptance the `PostToolUse` hook sets the build signal **and** injects a terse
   assistant-internal stance directive — do-not-relay machine context that re-grounds the session (which
   still holds its start-of-session explore briefing) and sends it into the build-orchestration kickoff. The
   **operator** meets build-entry exactly once, through that kickoff ("opening a draft pull request and
   planning the work") — never through the hook. The signal is the **sole durable record**: it is cleared to
   explore at every SessionStart, so a copy of that directive replayed on a resumed session is inert — the
   session reports its stance from the live signal (explore), and the kickoff proceeds only if the live
   signal still reads build. Neither path is silent or self-elected — the model never flips its own stance.
4. **Routine is unattended, scope-locked build work** entered by an operator-authored scheduled fire: a
   scheduled automation (a Claude Desktop routine or a Codex Automation) runs the routine command, which enters
   the Routine write-stance through `set-routine` — a **mechanical** gate that grants the stance only in a
   proven-isolated worktree, never the operator's checkout — and which the run additionally declines to enter
   when its start-of-session hooks did not fire (an honest-tier check the run follows, since on Codex the
   write-gate is itself a hook that can be off). It never merges the protected branch. It is the same workflow,
   constrained.

To check the live stance, `python tools/modes.py stance` — it resolves the session from `--session` or
`$CLAUDE_CODE_SESSION_ID`, and says `unknown` (non-zero) rather than a misleading `explore` when it cannot
resolve one. To see what the gate decides for any action without Claude Desktop (the operator demo): `python
tools/modes.py demo` (which also shows the plan-file carve-out and plan-acceptance setting build + injecting
the stance directive), or `python tools/modes.py classify <Tool> [command] [--session S] [--pm MODE]`.

## Done when

The session's stance is legible and enforced as a nudge: in explore, a building action is refused with a
plain sentence while a read or a test runs unimpeded; setting the build stance permits the same action;
clearing the signal returns the session to explore. The current stance is named in the status block the
assistant renders first each session ("Exploring — I won't change files…"), and a denied action is relayed
to the operator in plain words rather than refused in silence.

## Notes

The gate is a **deliberate-effort nudge, not a wall** — stated honestly, never overstated. The current
platform honors the gate's deny (emitted as the exit-0 + `hookSpecificOutput` form, across built-in and
GitHub-MCP tools); it is still fallible for two durable reasons: a crashing gate fails open (the action
proceeds, by design — a gate must never strand the operator), and detecting a build verb in a shell
string is best-effort (an alias, `eval`, substitution, or chaining evades it). The only unbypassable
guarantee is the **protected-branch merge** — any write that ever slips the gate (a crash, an evaded
verb, or a `permissions.allow` entry that outranks the hook, which is why the engine never allow-lists a
gated tool) still cannot reach the protected branch unreviewed. Never dress the local gate as the wall.

Entering build is fail-safe too: if the plan-acceptance hook never fires — including accepting a plan with
the context cleared, which does not fire it (claude-code#20397) — the signal stays absent → explore, and
`/engine-start` is the recovery. A miss can only leave the session in explore, never falsely in build.
