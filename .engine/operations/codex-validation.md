---
title: Validate the Codex adapter live — the post-merge pass bar, and the update re-trust ritual
---

## Purpose

Prove, in a live Codex session, the adapter behavior no check in this repository can prove from
inside (the platform's hook firing, discovery, and sandbox behavior only exist under a running Codex
binary — eADR-0034), and keep Codex sessions healthy across engine updates. Enter this runbook right
after the dual-runtime change merges (the named acceptance step), after any later change to the
Codex adapter surfaces, or when a Codex session reports its hooks are not running.

## Steps

1. **Item zero — version.** Run `codex --version` and confirm the installed Codex is a build with
   hooks support (a 2026 build, around v0.114 or later). On an older build every later step fails
   for that reason alone — upgrade first, or stop here and say so.
2. Open the repository in Codex, run `/hooks`, and approve the engine's hooks (they are skipped
   until trusted; after any engine update that changes `.codex/hooks.json` they need re-approval —
   the engine says so whenever it changes that file).
3. Start a fresh session and check the floor and grounding: the session reads `AGENTS.md`, and its
   first reply opens with the **Project status** block (or plainly discloses that the briefing did
   not arrive and grounds manually via `uv run --directory .engine -- python tools/engine_status.py`).
4. Check the write-gate: ask for a small file edit WITHOUT starting a build — the edit must be
   denied with the plain exploring explanation; a shell `git commit` must be denied the same way.
5. Check Build entry: type `$engine-start` — the stance flips to building (and ONLY this typed verb
   does; casual phrasing must not).
6. **Check deferred live-helper discovery — including its failure branches, without disturbing the real
   project.** The exact Codex procedure is emitted by `.engine/tools/boot.py`
   `MCP_AVAILABILITY_CHECK_CODEX`; validate that procedure, never an invented substitute.
   - **Healthy deferred case, in this project:** start a fresh session and record whether the initial tool
     summary omits `mcp__engine_memory.health` and `mcp__engine_knowledge_graph.health`. When it omits them,
     confirm one search per helper discovers the exact tool, each fixed health call returns its exact server
     identity, and the first reply carries no helper-outage warning. If this Codex build surfaces either tool
     initially, that helper's deferred-discovery branch was **not verified** — do not call it a pass; repeat on
     a build/session that actually defers it.
   - **Controlled failure matrix, only in throwaway project copies:** never edit this project's real
     `.codex/config.toml`, trust state, or servers. Exercise these seven fresh-session cases explicitly:
     (1) both pass; (2) memory passes and knowledge discovery misses; (3) memory passes and knowledge is
     discovered but its call fails; (4) knowledge passes and memory discovery misses; (5) knowledge passes and
     memory is discovered but its call fails; (6) both discovery checks miss; (7) both are discovered but both
     calls fail. Produce a miss by omitting only that temporary registration. Produce a call failure by pointing
     only that registration at a temporary MCP fixture which registers the exact `health` operation but returns
     an MCP error — never damage a real store or server. In every mixed case the passing helper stays silent and
     only the failed helper warns; a discovery miss gives the trust-and-restart diagnosis, while a discovered
     call failure says registered-but-not-passing and does not blame trust. Remove temporary copies and fixtures
     afterward. If Codex cannot isolate them from the real project's trust state, record the negative arms as
     **not verified** rather than perturbing the real installation.
7. Check memory capture: after a turn or two, `$engine-status` shows no memory-capture warning (a
   "conversation wasn't saved" line means the transcript reader needs updating — a defect, not a
   deferral).
8. Check review reach: the ten personas under `.codex/agents/` are visible to the session and a
   spawned one reports without editing anything.
9. Check help: `$engine-help` renders the commands with the `$` prefix.
10. Check the routine backend (unattended work). Item zero (two platform facts the whole routine rides on,
    unverifiable from inside the repo): the installed Codex build supports Automations and a scheduled
    Automation fires SessionStart (you see the start-of-session briefing / a resolvable session); AND the
    Automation's "dedicated background worktree" is a git-linked worktree the isolation gate recognizes — i.e.
    `set-routine` **enters** Routine there, rather than declining "not a dedicated worktree" (if it declines,
    Codex is isolating by a means the gate doesn't yet detect — a defect owed a fix here, since the ledger
    exception was retired on the twin's presence, ahead of this live check). Then configure a Codex Automation with
    `$engine-routine`, a dedicated background worktree, `approval_policy = "never"` + `workspace-write`, and
    network access, pointed at a scope-locked build Issue with an open draft pull request. Confirm it enters
    **Routine** (the run reports "Running unattended (routine)…"), advances one planned chunk into the pull
    request, and **never merges**. Then confirm the safety refusals: pointed at your main checkout (worktree
    off), or with hooks un-retrusted after an update, it **refuses to write** and says why in the run output —
    no ungated or main-checkout writes.
11. **Check the from-Codex self-review (the read-only audit convenience).** Item zero (platform facts, unverifiable
    from inside the repo): a scheduled Automation fires and runs its pasted prompt; that prompt makes the run **adopt
    the audit persona** — it loads and follows `.claude/agents/engine-audit.md`, rather than musing generically; a
    `sandbox_mode = "read-only"` Automation genuinely blocks writes **and reads only within the project** (so the
    operator's out-of-repo saved memory at `~/.claude/.../memory/` is mechanically out of reach, not merely by the
    persona's discipline); and — for an operator who also runs the write-capable build routine — the review's
    read-only sandbox can be scoped to **this** Automation without disabling that routine's `workspace-write` (if
    Codex cannot scope the sandbox per Automation, record that limitation here). Then configure a **read-only** Codex Automation
    (`sandbox_mode = "read-only"` + `approval_policy = "never"`; no worktree, no network, no token) with the paste
    instruction from `.engine/audits/self-review-setup.md`, and **Run now**. Confirm all three: (a) a **plain-language
    self-review summary appears in the run** — what it looked at, found, and recommends; (b) it is the **real persona
    degrading honestly, not a look-alike** — the summary **discloses what it could not reach** (the saved memory, the
    engine's issue backlog, prior reviews, live soft findings, and — only where the project has settled a `docs/spec/`
    — the spec-conformance feed), since a committed-files-only
    run is handed none of them; a summary that claims to have reviewed those, or quotes a specific saved-memory note,
    is the tell that the paste loaded something generic **or** that the read-only sandbox reached your local memory —
    either is a defect owed a fix here; (c) it **writes nothing** — no file edit, no commit, no pull request, no
    Issue. This arm is a convenience — its findings live in the run; the scheduled GitHub self-review stays the
    dependable, durable-findings path.

## Done when

Every step above passed in a live Codex session — or each failure is recorded as a defect owed an
immediate fix in this line of work (a failure inside this bar is never re-scoped as a follow-up). With the
routine adapter shipped, the provider-exception ledger carries no remaining capability follow-up — every
engine command now has its Codex twin. The from-Codex self-review (step 11) rides the audit persona's
existing twin as a read-only convenience — its findings surface in the run, not a filed record — so it opens
no new capability the ledger must track.

## Notes

The honest split this runbook exists for: everything above rides the platform's own behavior, which
the repository's checks deliberately do not simulate — they prove the committed files are coherent,
in sync, and parity-complete, and THIS pass proves the platform actually consumes them. The
protected main branch and the operator's merge remain the only wall on every runtime; the hooks are
guardrails (Codex's own documentation says its pre-tool hook is not a complete enforcement
boundary, recorded in the exception ledger). Windows behavior is untested by this project and stays
so until someone runs this pass there.
