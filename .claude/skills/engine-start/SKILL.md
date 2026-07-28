---
name: engine-start
description: Start building — switch from looking around to making changes, which I'll put up for your approval.
invocation: operator-typed
disable-model-invocation: true
allowed-tools: Bash(uv run *)
---

## Steps

1. Switch this session into building by running:
   `uv run --directory .engine -- python tools/modes.py set-build --session "${CLAUDE_CODE_SESSION_ID}"`
   (the engine works out this session's identity automatically. If the command reports it could not
   identify the session, say so plainly: the stance stays as it was, and building has not started.)
2. Tell the operator, in plain words, that the session is now building — say: "Building — I'll make changes
   and submit them as a pull request for your approval."
3. Begin the work by following the build procedure in `.engine/operations/build-orchestration.md` — open
   the draft pull request and plan the work, then show the operator the risk assessment and get their
   how-careful depth choice, before changing anything.

## Notes

This is a command you type to begin building. I won't start building on my own — that is your call: type
`/engine-start` and the work begins. (In Claude Code, approving a plan I've shown you also starts it; in
Codex, the typed command is the only way in.)
