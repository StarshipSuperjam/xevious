---
title: Remove the engine — take it out cleanly and leave a working, engine-free project
---

## Purpose

How the engine removes **itself** entirely, leaving a project that still works without it. The removal is
**reviewed** (it arrives as a pull request you approve, never a silent change) and **reversible** (undoing
that pull request brings the engine's files back). It does three things in the order safety demands: it takes
the engine's checks off your main branch's safety rule **first** — so the next step can't get stuck — then it
removes the engine's entries from your shared setup files and deletes the engine's own files, and finally it
opens a pull request with all of that for your review. Because the engine set up a safety rule on your main
branch, removal lets you **choose** what happens to that rule: keep it (your main branch stays protected, just
without the engine's checks) or remove it entirely. The engine never removes that protection without you
choosing. Enter this runbook to understand or perform removing the engine. The tool is
`tools/module_manager.py`.

## Steps

1. **Choose what happens to your main-branch safety rule.** The engine set up a rule that requires a pull
   request and passing checks before anything reaches your main branch. Removing the engine takes the engine's
   checks out of that rule. Decide whether to **keep** the rule (protected, minus the engine's checks) or
   **remove** it entirely. Keep it unless you're sure you want it gone.
2. **Remove the engine.** `module_manager.py remove-engine --confirm` with either `--keep-protection` or
   `--remove-protection` takes the engine's checks off your safety rule, removes the engine's entries from your
   shared setup files (leaving anything that might also be yours), deletes the engine's own files, and opens a
   pull request containing the deletions. Run without `--confirm` first to see what it will do — that preview
   changes nothing.
3. **Review and merge.** The removal lands as a pull request. Until you merge it, the engine's files are still
   present. Merging it is your approval; reverting it brings the files back.

## Done when

The engine's files are gone, your shared setup files keep only your own entries, your main-branch safety rule
reflects the choice you made, and the removal pull request is merged — leaving a working, engine-free project.
Or, if the removal could not start (the safety rule couldn't be reached), you have been told plainly why, with
nothing changed.

## Notes

**Reviewed and reversible.** The file deletions arrive as a pull request behind your normal review, so you
approve them and can undo them by reverting that pull request.

**Your main-branch safety rule, and the gap while removing.** The engine takes its checks off the safety rule
**before** opening the deletion pull request — it has to, because a check whose files are being deleted would
otherwise block that very pull request forever. So from that moment until you finish: if you chose to **keep**
the rule, your main branch is still protected but without the engine's checks; if you chose to **remove** it,
your main branch is no longer protected, and turning protection back on later means running the engine setup
again to re-create the rule. Reverting the removal pull request brings the files back but does not turn the
safety rule back on by itself.

**When the safety rule was yours, not the engine's.** If the engine arrived on a repo that already protected
its main branch and added its checks to *your* rule (rather than creating its own), removal takes back exactly
what it added — its checks, and any force-push/deletion/pull-request protection it had to add — and leaves the
rest of your rule exactly as it was. There is no keep-or-remove choice in that case, because the rule is yours,
not one the engine created; the engine never deletes a rule it did not make.

**Shared things the engine leaves alone.** In your shared setup files the engine removes only its own entries.
If something there might also be yours — a permission you granted that you may still want — the engine leaves
it and tells you, so you can remove it by hand if you don't need it. The cost of never wrongly removing
something of yours is that a little may be left behind, named for you.
