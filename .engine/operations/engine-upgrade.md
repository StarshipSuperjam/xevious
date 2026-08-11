---
title: Update the engine — fetch the newer released version and install it, reviewably and reversibly
---

## Purpose

How the engine updates itself to a newer released version, with three promises that make the update safe to
trust: it is **reviewed** (the update arrives as a pull request you approve, never an in-place change), it is
**reversible** (undoing that pull request undoes the update), and it **degrades** (if the newer version can't
be reached, the engine simply stays on the one it has and keeps working). The engine is detached from where
it came from, so an update is not a normal pull — it is fetched from the engine's published releases, pinned
to one tagged version. The update replaces the engine's **own** code while keeping **your** settings and your
saved data; reshapes any saved data that the new version needs in a different form — **backing it up first**;
re-checks that everything still fits together; and opens the result for your review. Enter this runbook to
understand or perform an engine update. The tool is `tools/module_manager.py`. The update refuses cleanly,
changing nothing, whenever it cannot proceed — so it is safe to try.

## Steps

1. **See where you stand — and what an update would change.** Type `/engine-upgrade` (the operator command
   for the whole flow), or run `module_manager.py upgrade` directly. Either way it **only checks — it changes
   nothing**: it tells you the version you're on, whether a newer one is available, whether a previous update
   looks unfinished, and — when an update is available — **what that update would change**: the engine files
   it replaces or adds, the settings it turns on or off or updates, any stored-data change (and whether a
   backup is set up for it), and **any capability the update retires** — something you could ask the engine for
   before and no longer can, named in plain language, whether the update retired it *inside* a module it keeps
   or by dropping a *whole* module (a whole-module drop is retired cleanly on update, its files removed, not a
   refusal); and — the mirror of a removal — **any new capability the release brings in that you don't have
   yet**: a *required* one this version needs (installed automatically), a *default* add-on (turned on unless
   you decline it before merging), and any *optional* add-on it makes available for you to enable. To see this
   it fetches the new version's files read-only; nothing is applied.
   (`module_manager.py status` also lists the installed modules and the current version.)
2. **Apply the update — deliberately.** `module_manager.py upgrade --confirm` (optionally name a specific
   version) fetches the newer released version, replaces the engine's own files with it while **keeping your
   settings and saved data untouched**, turns shared-file settings on or off to match the new version,
   **installs the new modules the release adds that you need** — a required capability automatically, a net-new
   default add-on turned on unless you decline it — while **offering** the optional ones for you to add later,
   rebuilds the engine's tools, reshapes any saved data the new version needs in a new form, re-checks that
   everything fits together, and opens the change as a pull request. (If the release adds a *required* module the
   update cannot install, it **refuses cleanly and opens nothing** — an engine is never shipped missing a
   capability it requires.) Applying **takes the `--confirm`** — bare
   `upgrade` (and `upgrade --help`) never starts a real update. The release is fetched from the engine's
   **update home** — the repository the engine updates from (see Notes) — never from this repository's own
   remote. It is refused, in plain language, and **nothing is changed**, if no update home is recorded (the
   engine tells you plainly and asks you to record it, rather than guess one), if the home has no such release
   — it may have been renamed or removed (the engine names the home so you can check it), if the network can't
   be reached (the engine stays on its current version), if a needed change to saved data can't be backed up
   first (see Notes), or if a module you have is missing from the release *without* being recorded as an
   intentional removal — a broken or incomplete release (a module the release intentionally drops, recorded in
   its own removal record, is retired cleanly and announced, not refused).
3. **Review and merge.** The update lands as a pull request with the engine's checks. Merging it is your
   approval; reverting it undoes the update. Until you merge, nothing about the running engine has changed.

## Done when

The engine is on the new version, your settings and saved data are preserved, and the update's pull request is
merged — or, if the update was refused, you have been told plainly why (no update home is recorded, the home
has no such release or was renamed or removed, the network couldn't be reached, a needed saved-data change had
no backup set up, or a module you have vanished from the release without being recorded as an intentional
removal), with nothing changed.

## Notes

**The operator command, and how a mention of "upgrade" is handled.** `/engine-upgrade` is the command you
type to run the whole flow: it checks, shows you exactly what an update would change, and — only after you say
to go ahead — applies it as a pull request you review. "The `upgrade` command" throughout this runbook means
that typed command (`module_manager.py upgrade`, and `... --confirm` to apply) — never the word "upgrade"
spoken in conversation. If you simply mention wanting to update, the engine **points you to
`/engine-upgrade`** rather than running anything. This routing rests on three layers, named honestly:
- the `/engine-upgrade` command **cannot be started by the engine on its own** — only you typing it begins it
  (a firm mechanical limit);
- a conversational "I want to upgrade" is answered by pointing you to the command, not by acting on it (an
  instruction the engine follows — a rule, not a lock);
- and under both, **applying only ever opens a pull request** — nothing about the running engine changes
  until you merge it. So even if that middle rule were ever slipped, the worst outcome is a pull request you
  can simply reject.

**What the check covers.** The check answers four things before you commit to an update: whether an update is
**available** and to which version; the **impact** it would have (the files, settings, stored-data changes, and
retired capabilities above); the **progress** of applying it, which the apply step reports as it goes — what it applied, the data
it migrated, and the pull request it opened; and the **validation** — the engine's own consistency check runs
at the end, and, with the pull request's own checks, is visible on the pull request you review.

**Saved data is backed up before it is changed — or the update stops.** Most updates only replace the
engine's code, which a reverted pull request restores on its own. When an update also needs to change saved
data, it makes a backup first, so the change can be undone. If no backup is set up yet, the update **refuses**
that step rather than risk data it can't restore — ask the engine to set up a backup, then update again. And if an update is
undone *after* it already changed saved data, the engine notices on its next start and tells you, in plain
language, the exact command to restore the backup so your data and the engine match again.

**What an update replaces, and what it keeps.** An update refreshes the engine's **own** files — its tools,
checks, and the templates that guide your pull request and issue descriptions — while keeping **your**
settings and saved data untouched. So if you've edited one of the engine's own templates, an update replaces
it with the new version's wording; you can see and undo that in the update's pull request, like any other change.

**Where updates come from — your engine's update home.** Your engine is detached from the repository it was
created from, so updates don't arrive by an ordinary pull — they are fetched from the engine's **update home**,
the repository whose published releases your engine updates from, recorded once as part of your engine's own
record. Because that home decides where your engine's own code comes from, **changing it to a different home is
treated as a change to a safety setting**: the engine flags it at review and it takes your deliberate
acknowledgment to approve, exactly like any other change that could weaken a safety gate. If your engine has no
home recorded yet (an engine set up before this was added), it tells you plainly at the start of a session and
offers to record it — updates simply wait until it's set, rather than the engine guessing a home.

**Degrades, never pretends.** If the network can't be reached, the engine stays on the version it has and keeps
working; the update is simply not applied, and it says so. This is different from a home that has **no such
release** — renamed, removed, or with nothing published yet: there the engine names the home and asks you to
check it, rather than quietly waiting, because the home itself may be wrong.

**If an update stops half-applied, you can finish it or undo it.** An update installs the new version's files
first, then applies its settings and re-checks consistency. If it stops partway, the working copy is changed
but **nothing was opened for review or merged** — safe either way. `/engine-upgrade` shows both choices:
- **Finish it** — `module_manager.py upgrade --confirm` **again**; the second run uses the just-installed
  version's own logic to complete the stalled step.
- **Undo it** — `module_manager.py rollback --confirm` puts the engine back the way it was. It **saves a
  recovery point** of your current state first (nothing is lost), **refuses** if you have unrelated unsaved
  work of your own (asking you to set it aside first), and puts back any saved memory the update changed.

Bare `upgrade` reports a half-finished tree as *unfinished*, not "up to date", and bare `rollback` shows the
same choice — so you can always tell where you stand.

**Too old to update cleanly.** Before it changes anything (and in the preview), an update checks whether your engine
is below the release's **clean-upgrade floor** — the oldest version proven to update to it in one clean pass. Below
it the update **refuses and changes nothing**, names both versions, and says plainly to stay put for now (undoing
any earlier half-applied update first): an engine that old carries earlier update code that predates the
file-tidying machinery, so an automatic clean path from it isn't available. An honest stop, not a failure.

**Undoing an update you've already merged.** This can't be undone locally — the engine never rewrites your main
line. Instead its pull request is reverted (a normal reviewed change you merge — "reverting the pull request
undoes the update"). Once the code is back, the saved memory from before the update is put back (`rollback
--confirm`, or the engine's offer at the next start). That last step needs your backup reachable; if it isn't,
your memory is left unchanged and the engine offers again later — your code is safely back either way.

**New capabilities a release adds — installed or offered, never silently left off, never resurrected against
your wishes.** A new version can make modules available your deployment doesn't have (the mirror of a
whole-module removal), handled by what the release marks each: a **required** capability is installed
automatically — the version needs it, and if it *can't* be installed the whole update refuses rather than open a
broken pull request; a **default** add-on new to your deployment is turned on **unless you decline it** (tell me
before you merge and I'll remove just that one, or `module_manager.py remove <id>` later); an **optional** add-on
is only **offered** — nothing installs until you ask, with `module_manager.py add <id>`. What an update never
does is turn back on a module you deliberately declined or removed: a default add-on that was offered before and
isn't installed is treated as declined and only offered again. Each is disclosed in the preview and the pull
request, so you weigh it at the merge; a half-applied install takes the same finish-or-undo choice above.

**The required safety checks keep their names across versions**, so an update can never break the review gate
that protects the project.
