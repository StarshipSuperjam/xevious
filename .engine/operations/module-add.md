---
title: Add an engine module — fetch it from the engine's release and install it, safely
---

## Purpose

How the engine adds one module cleanly: it fetches the module's files from the engine's current released
version, copies them into place, turns on the shared-file settings the module needs, records it in the
engine's list, updates which dependency groups the tool-runtime installs, and re-checks that the installed
set is consistent — while refusing, in plain language, to add a module that is already installed or one
whose required companion module is missing. Enter this runbook to understand or perform adding a module. The
tool is `tools/module_manager.py`. Adding a module needs no special permission and never touches the
branch-protection setting — it changes only what runs *inside* the engine's existing checks, not the checks
themselves. (Re-adding a module that was turned off during first-time setup uses this same path — its files
were deleted then, so they are fetched again now; it is an install, not a toggle.)

## Steps

The operation refuses cleanly, changing nothing, whenever a module cannot be added — so it is safe to try.

1. **See what is installed.** `module_manager.py status` lists the installed modules and what each one
   needs, so it is clear up front whether the module is already present and whether the companion modules it
   needs are installed.
2. **Add it.** `module_manager.py add <module>` fetches the module's files from the engine's current
   released version, copies them into their places, turns on the shared-file settings it declares (such as a
   dependency-cache ignore line, an extra tool, or a registered helper), records it in the engine's list at
   its version, updates the tool-runtime's dependency-group selection, and re-checks the result. It is
   refused, in plain language, if the module is already installed, if the fetched files do not match the
   requested module, or if a module it needs is not present or is the wrong version — and nothing is changed.
3. **Confirm what was installed.** The engine reports the files it added, the settings it turned on, the
   updated dependency-group selection, and whether the installed set is consistent. A clean result means the
   installed modules fit together; any problem is surfaced with the next step to take.

## Done when

The module's files are in place, the shared-file settings it needs are turned on, the engine's list records
it at its version, the tool-runtime's dependency-group selection includes it, and the installed set is
reported consistent — or, if the add was refused, the operator has been told plainly why (already installed,
a mismatched fetch, or a missing companion module), with nothing changed.

## Notes

**Adding a module touches no branch-protection setting.** A module's checks flow in and out of the engine's
stable required check by which check files are present, so adding a module changes only what runs inside that
check, not its name — no operator-privileged step is needed. (A module that ships its *own* separate required
check, and updating or removing the whole engine, are different steps other capabilities own.)

**Where the files come from.** The files are fetched from the engine's current released version, pinned to
that exact version — never an in-progress copy — so an add installs files that match the engine the
repository already runs. If the release cannot be reached, the add reports that plainly and changes nothing.

**When to reactively offer a module — the offer, never a silent install.** Most adds start with the operator
asking. But the engine may also *offer* one: when an operator's request maps to a capability that an **installed
optional package does not provide but an uninstalled one would**, offer to add that module via this same add
path — say what it turns on and let the operator decide. This is always an **offer**, never a silent install:
the operator's yes is what installs it, exactly as when they ask directly (the adopter floor — the root
`CLAUDE.md` — carries this as a standing rule). The threshold is judgment, kept deliberately plain so it
guides without a brittle rule: offer when the request **clearly** maps to what an uninstalled module is built
for — a direct ask for its capability, or the same need coming up more than once — not on a faint keyword brush
that would turn every mention into a prompt to install. When unsure, name the capability and ask, rather than
either installing or staying silent. Run `module_manager.py status` first (as in step 1) to confirm the
capability really is uninstalled before offering.
