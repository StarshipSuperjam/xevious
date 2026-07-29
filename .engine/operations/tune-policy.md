---
title: Tune an engine setting — adjust a tuning value through a reviewed change
---

## Purpose

Let a non-engineer change one of the engine's tuning numbers — how patient its background monitoring is, or
how it weighs what to show first — without hand-editing a file. Enter this whenever the operator types
`/engine-tune` or asks to change one of these settings. The end state: the operator's choice is saved and
prepared as a pull request they approve, so it takes effect only on their say-so and survives an engine
update.

## Steps

1. **Show what can change.** Run `uv run --directory .engine -- python tools/tune.py show <group>` for the
   group the operator cares about, and present the settings and their current values in plain words. The
   groups are `triage-threshold` (how patient the background monitoring is before it flags a recurring
   signal), `contract-threshold` (the decision-record burst signal), and `attention` (how the engine weighs
   what to surface first).
2. **Take the operator's choice** — which setting, and the new number they want.
3. **Reassure before saving.** Tell the operator, plainly: "This won't change anything on its own — it
   prepares your change as a request you approve before it takes effect." Confirm they want to proceed.
4. **Save and prepare the change.** Run
   `uv run --directory .engine -- python tools/tune.py set <group> <setting> <number>`. If the value is not a
   number, or names a fixed safety setting, the command refuses in plain words — relay that to the operator
   and offer a setting they can change instead. Do not retry a refused change.
5. **Confirm and hand off.** Tell the operator, plainly: "I've prepared your change as a pull request — open
   it and merge it to make it take effect. Nothing changes until you do." Point them to the pull request.

**Clearing a setting the engine no longer has.** An engine update can retire a setting — the behaviour it
governed changed, so there is no longer a number to set. The saved-settings check then flags the operator's
old value on their next change, because a choice they made has stopped applying and they should hear it from
the engine rather than discover it. An update normally removes the entry itself; when one has survived, run
`uv run --directory .engine -- python tools/tune.py forget <group> <setting>`. It prepares the removal as a
pull request exactly like any other change. Tell the operator **why** it was retired (the command prints the
reason when the engine has one recorded) and that clearing it changes nothing about how the engine behaves —
the value was already being ignored. The command refuses to clear a setting that is still adjustable, and says
so — that is a `set`, not a clear. It *will* clear a saved value for one of the fixed safety settings, which
`set` also refuses: that value can never apply, so leaving it would keep failing the check with no way out.

## Done when

The operator's choice is saved to the engine's saved-settings file and prepared as a pull request for their
approval — or the change was refused in plain words (not a number, or a fixed setting) and the operator was
offered a setting they can change instead. Either way, nothing has taken effect yet: a saved change takes
effect only when the operator merges the pull request.

## Notes

The saved-settings file is kept in a place an engine update does not touch, so a tuned value is not undone by
an update. Settings that keep the engine's safety order — the order in which it always handles the most
important things first — are fixed on purpose and cannot be changed here; the command lists only the settings
that can. Some settings only take visible effect once the part of the engine that reads them is switched on
in a later part of the engine — saving them now is safe, and they apply when that part is live.
