---
title: Author a code of conduct — add, revise, or retire how the AI works with you, through a reviewed change
---

## Purpose

Let a non-engineer shape the standing behavioral stance — the codes of conduct that say how the AI engages
(plain language, explaining before acting, pushing back, and the like) — without hand-editing a file blind.
Enter this whenever the operator types `/engine-conduct` or asks to change how the assistant works with them.
The end state: the operator's change to their own codes of conduct is written and prepared as a pull request
they approve, so it takes effect only on their say-so and survives an engine update.

## Steps

1. **Show the current stance.** Read `.engine/conduct/defaults.md` (the engine's universal codes) and
   `.engine/conduct/operator.md` (the operator's own). Present the active stance in plain words — each code's
   title and rule — noting which are engine defaults and which are the operator's, and that an operator code
   with the same id as a default takes priority.
2. **Take the operator's intent** — add a new code, revise one of their own, retire one of their own, or
   disable an engine default that doesn't fit. Draft the wording *with* them: plain language, first person
   ("I …"), one or two sentences. A code is posture, never a gate — never draft one that purports to skip a
   review, auto-approve a change, treat built-in memory as authoritative, or weaken a guardrail (the engine
   flags that at the merge).
3. **Reassure before writing.** Tell the operator, plainly: "This won't change anything on its own — it
   prepares your change as a request you approve before it takes effect." Confirm they want to proceed.
4. **Write the change to the operator layer.** Edit `.engine/conduct/operator.md` only (never the engine
   defaults): add or revise the `codes` entry (a `conduct-<slug>` id, a title, `status: active`) together with
   its matching `## <title>` section; to drop an engine default, add its id to the `disables:` list; to retire
   one of their own, set its `status` to `retired` or remove both its entry and section. Keep the set small —
   each code earns its place.
5. **Confirm and hand off.** Prepare the change as a pull request and tell the operator, plainly: "I've
   prepared your change as a pull request — open it and merge it to make it take effect. Nothing changes until
   you do." Point them to the pull request.

## Done when

The operator's code of conduct is written to their own override file and prepared as a pull request for their
approval — or no change was made because they decided not to proceed. Either way nothing has taken effect yet:
the change applies only when the operator merges the pull request.

## Notes

Codes of conduct are guidance the AI follows, not a safety gate — they can never skip a review or weaken a
guardrail, and the engine warns at the merge if a code's wording reads that way. The operator's override file
is kept in a place an engine update does not touch, so a code they set is not undone by an update; the engine's
own default codes may improve across updates. To carry a code into every project made from this template, an
optional step promotes it into the template's seed (`.engine/provisioning/conduct-seed.md`) so future projects
start with it — offer this only when the operator asks to reuse a code everywhere.
