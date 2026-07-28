---
title: Remove an engine module — undo its settings and delete its files, safely
---

## Purpose

How the engine removes one installed module cleanly: it reverses the shared-file settings that module
added, deletes the files it owns, drops it from the engine's record, and re-checks that what remains is
consistent — while refusing to remove a module another module still needs, and being honest about the one
thing it deliberately leaves behind. Enter this runbook to understand or perform an ordinary module
removal. The tool is `tools/module_manager.py`. An ordinary removal needs no special permission and never
touches the branch-protection setting — it changes only what runs *inside* the engine's existing checks,
not the checks themselves. (Removing the *entire* engine is a different, larger step a separate capability
owns — the `remove-engine` verb and its own `engine-remove.md` runbook; this runbook is for removing one
module.)

## Steps

The operation is safe to inspect first and safe to re-run — removing a module that is already gone simply
reports that there is nothing to remove.

1. **See what is installed and what depends on what.** `module_manager.py status` lists the installed
   modules, which modules each one needs, and which ones need it — so it is clear up front whether a module
   can be removed on its own.
2. **Check that removal is safe (read-only).** `module_manager.py plan-remove <module>` reports whether the
   removal would be refused and why. It is refused, in plain language, if another installed module still
   needs the one being removed (remove that one first) — and a required, foundational module cannot be
   removed on its own at all. Nothing is changed by this step.
3. **Remove it.** `module_manager.py remove <module>` reverses the module's shared-file settings (the hooks,
   the dependency-cache ignore lines, an MCP server it registered), deletes the module's own files and its
   folder, drops it from the engine's record, and updates which dependency groups the tool-runtime installs.
   Identical re-runs change nothing.
4. **Read what it left in place.** If the module had added a permission, the engine leaves that permission
   alone and says so plainly — it cannot be sure the permission is the engine's alone and not also the
   operator's, so it never removes a shared one. The operator can remove it by hand if it was only for that
   module.
5. **Confirm what remains is consistent.** The engine re-checks the remaining set and reports it in plain
   language. A clean result means the remaining modules are consistent; any problem is surfaced with the
   next step to take.

## Done when

The module's shared-file settings are reversed, its files and its folder are gone, the engine's record no
longer lists it, the tool-runtime's dependency-group selection matches the remaining modules, and the
remaining set is reported consistent — or, if removal was refused, the operator has been told plainly which
module blocks it and what to do, with nothing changed. Any permission deliberately left behind has been
disclosed, not hidden.

## Notes

**Ordinary removal touches no branch-protection setting.** A module's checks flow in and out of the engine's
stable required check by which check files are present, so removing a module changes only what runs inside
that check, not its name — no operator-privileged step is needed. Removing the *entire* engine is different:
it must also turn off the engine's required-check binding (an operator-privileged step) so a leftover
binding to a deleted check cannot deadlock the repository's own pull requests. That whole-engine removal,
and adding a module back (which fetches it from a release), are separate capabilities with their own
runbooks — see `engine-remove.md` and `module-add.md`.

**The honest residue.** A bare permission a module added cannot be proven to belong to the engine alone, so
removal leaves it rather than risk removing one the operator wanted — the accepted cost of never removing
the wrong thing. It is always disclosed, never silently left.
