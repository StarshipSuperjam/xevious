---
name: engine-status
description: Show where your project stands — what's next, what recently shipped, and anything that needs your attention.
invocation: operator-typed
disable-model-invocation: true
allowed-tools: Bash(uv run *)
---

## Steps

1. Show where the project stands by running:
   `uv run --directory .engine -- python tools/engine_status.py --session "${CLAUDE_CODE_SESSION_ID}"`
   (the engine works out this session's identity automatically. If no session can be identified, the
   status still renders — only the building-or-looking-around line may be left off.)
2. Show the operator the status exactly as it is printed — where the project stands, what recently shipped,
   and anything needing their attention. Do not summarize or reword it.

## Notes

This is a command you type any time to see where your project stands. It only reads; it never changes
anything.
