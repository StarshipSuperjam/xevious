---
title: Set up a GitHub Projects board — connect a one-way progress board the engine keeps in step
---

## Purpose

How the engine connects a GitHub **Projects** board so you can see, at a glance, what is being built,
what is next, what needs your review, any known issues, and when the board was last refreshed. The board
is a **one-way mirror over the real record** — your issues, pull requests, and the engine's own state stay
the source of truth, and you can delete the board any time and lose nothing. The engine keeps only its own
fields in step; **your Status changes, your card moves, and your own board text are always yours and are
never overwritten.** A board lives on your GitHub account and is only linked to this repo, so it is not
created when the repo is — this runbook creates and connects it. The tool is `tools/projects_sync/`. Enter
this runbook to set up the board or to reconnect one. Nothing here runs automatically; you (or the engine on
your say-so) run each step, and any step can be skipped or undone.

## Steps

Each step is a precondition of the next. The board sync runs through your own logged-in `gh`, so **no token
is ever stored in the repo.**

1. **Grant the one-time `project` permission — your call.** GitHub will not let the engine create a board
   without it. Run `gh auth refresh -s project`. Be clear-eyed about what this grants: it lets the engine
   **read and change every GitHub Project on your whole account — not just this repo's board.** It is
   optional, and you can take it back at any time with `gh auth refresh --remove-scopes project`. If you are
   not comfortable granting it, stop here — the engine keeps working from your issues and pull requests
   exactly as before.
2. **Create the board.** `gh project create --owner @me --title "<your project> — engine"`. Note the board
   **number** and **id** it prints (use `--format json` to capture the id).
3. **Link it to this repo.** Run `gh project link <number> --owner <your-login> --repo <repo-name>` — name
   your login and the repo explicitly, not `@me`. (`gh api user --jq .login` prints your login;
   `gh repo view --json name --jq .name`, run from inside this repo, prints the repo name.) `@me` works as
   the owner in the steps above, but linking also resolves a *repository*, and `@me` fails at that step with
   *"Could not resolve to a Repository"* — so name them explicitly here. This is the form `gh`'s own docs use
   (`gh project link 1 --owner monalisa --repo my_repo`).
4. **Create the engine's five fields.** The engine writes only into fields it owns, so create them once (any
   names work, but these are what it looks for): for each of **What's being built**, **What's next**,
   **Needs your review**, **Known issues**, and **Last synced**, run
   `gh project field-create <number> --owner @me --name "<name>" --data-type TEXT`.
5. **Connect the board to the engine.** Run `tools/projects_sync/projects_sync.py resolve <project-id>`. It
   reads the board's current field ids into a local, gitignored settings file and confirms which engine
   fields it found. From now on the engine refreshes those fields at the start of each session.
6. **Turn on auto-add (optional) — a manual click only you can do.** GitHub offers no command to switch this
   on, so the engine cannot do it for you. In the board's **⋯ → Workflows → Auto-add to project**, enable it
   and set the filter to this repo. Then run `tools/projects_sync/projects_sync.py check` to read back
   whether it is on. **Skipping this is fine:** the board still shows everything the engine is working on
   either way — you just won't see your *other* (non-engine) issues and pull requests appear on it
   automatically, and you can always add those yourself.

## Done when

`tools/projects_sync/projects_sync.py check` reports the board is connected, and the engine's five fields
appear on it. The board then refreshes at the start of each session (about every fifteen minutes at most).

## Notes

- **If your account can't make user projects** (an org policy, or you lack permission in an org), the create
  step will say so — ask whoever administers the org, or use a board on your own account. The engine never
  dead-ends here; it just keeps working from the issues and pull requests.
- **Removing this later** reverses the engine's settings and deletes its files, but the **board itself, the
  cards the engine added, and the `project` permission stay on your GitHub account** — the engine cannot
  reach back out to delete them. Delete the board yourself if you want it gone, and run
  `gh auth refresh --remove-scopes project` to take the permission back.
- The engine only ever **adds its own already-labelled work** and writes **its own five fields**. It never
  changes a card's Status, column, or position — those are yours and GitHub's built-in automation's.
- **The engine's five fields are its own, and it keeps them in step with the real record every session.** So
  if you type your own value into one of them — say a different *Known issues* count — the engine will set it
  back to match the real record at the next sync. That is expected, not a bug, and it's called out here so it
  never surprises you: to change what those five fields show, change the underlying work they mirror (the
  issues, pull requests, and engine state), not the board cell. Everything else on the board — your Status,
  your card moves, your own board text — stays yours and is never touched.
