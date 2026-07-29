---
name: engine-design
description: Describe what you want to build, in plain words — I'll help you write it down clearly, check it holds together, and settle it as the description to build from.
invocation: operator-typed
disable-model-invocation: true
allowed-tools: Bash(uv run *), Bash(gh *)
---

## Steps

1. Read the runbook `.engine/operations/product-intake.md` and follow it with the operator, one step at a
   time: lay out the pieces of what they want and confirm the shape together, agree on how much detail to
   capture now, write it up under `docs/spec/`, check that every part is present and well-formed, report that
   plainly, and record the operator's go-ahead. Keep the procedure in the runbook — this command is just the
   way the operator starts it.

## Notes

This is a command the operator types to describe what they want built — it is never started on the engine's
own initiative. Everything it produces is plain, readable files inside the operator's own project; nothing is
treated as settled until they say so, and a settled description can always be changed later — the engine just
asks the operator to confirm the change on the pull request first (by applying the `guardrail-ack` label), so it
is never a quiet edit. If the project's GitHub connection isn't reachable, the runbook still captures the work as
committed files and says so, rather than stopping. Once a description is settled, the same command can turn it
into a build order and a list of things to build — the work a build picks up from.
