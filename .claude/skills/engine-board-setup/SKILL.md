---
name: engine-board-setup
description: Set up a GitHub Projects board that shows, at a glance, what the engine is building.
invocation: operator-typed
disable-model-invocation: true
allowed-tools: Bash(gh *), Bash(uv run *)
---

## Steps

1. Read the runbook `.engine/operations/projects-sync-setup.md` and follow it with the operator, one step
   at a time: grant the one-time `project` permission, create the board, link it to this repo, create the
   engine's five fields, connect the board with
   `uv run --directory .engine -- python tools/projects_sync/projects_sync.py resolve <project-id>`, then
   optionally turn on auto-add and read it back with `… projects_sync.py check`.
2. Before the permission step, make sure the operator hears it plainly: the `project` permission lets the
   engine read and change **every** GitHub Project on their account, not just this repo's board — it is
   optional and revocable (`gh auth refresh --remove-scopes project`). Do not run it for them without that.
3. When done, confirm with `… projects_sync.py check` and tell the operator the board now refreshes at the
   start of each session.

## Notes

This is a command you type to connect a progress board. The board is a one-way mirror: the engine keeps
only its own five fields in step with the real record every session and never touches your Status or card
moves. Tell the operator plainly that because those five fields are the engine's own, a value they type into
one will be refreshed back to match the real record at the next sync — to change what those fields show,
change the underlying issues, pull requests, and state, not the board cell. You can skip the board entirely
— the engine works the same from your issues and pull requests — and you can delete the board later without
losing anything. Removing the board or the permission is something you do on GitHub yourself; the engine
cannot reach back out to undo those.

At the link step, name the operator's login and the repo explicitly (`--owner <login> --repo <repo>`), not
`@me`: `@me` works as the owner in the create/field steps, but linking also resolves a repository and `@me`
fails there ("Could not resolve to a Repository"). The runbook's step 3 carries the working form.
