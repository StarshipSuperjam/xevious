---
name: engine-conduct
description: Shape how I work with you — add, revise, or retire a code of conduct, then put your change up for your approval.
invocation: operator-typed
disable-model-invocation: true
allowed-tools: Read, Edit, Write, Bash
---

## Steps

1. Follow the procedure in `.engine/operations/conduct-author.md`. In short: show the operator their current
   codes of conduct (engine defaults plus their own), draft the change with them in plain language, write it to
   their own override file (`.engine/conduct/operator.md`), and prepare it as a pull request they approve. Tell
   them, in plain words, that nothing changes until they merge it.

## Notes

This is a command you type to change how I work with you — the plain-language "codes of conduct" I follow each
session (speaking plainly, explaining before I act, pushing back, and the like). It never changes anything on
its own: your change is saved as a pull request you approve, and kept in a place an engine update won't undo. A
code of conduct is guidance, never a safety gate — it can't skip a review or weaken a guardrail.
