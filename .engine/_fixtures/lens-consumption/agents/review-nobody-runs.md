---
name: review-nobody-runs
role: plan-review
lens: orphaned-review
model-tier: judgment
permissions: read-only
disallowedTools: [Edit, Write, NotebookEdit, Bash]
output-contract: plan-review-finding.v1
---

# review-nobody-runs (negative fixture)

A negative fixture for `engine/check/lens-consumption`: this plan-review persona declares lens
`orphaned-review`, which no build stage consumes — it is absent from the `consumed-review-lenses` block
in `.engine/operations/build-orchestration.md`. So the review is installed yet no stage runs it against
the operator's changes, and the lens-consumption guard must catch it. Kept under `.engine/_fixtures/`
(not `.claude/agents/`) so Claude Code's own agent loader never picks it up as a real persona.
