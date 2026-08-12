"""The github-projects-sync module's tool package (optional, adopter-facing Product Management).

This package projects the repo-authoritative work signal (the committed state cursor, attention's
ordering, and the open-engine-issue debt) onto a GitHub Projects v2 board for a non-engineer's
at-a-glance visibility — a one-way, replaceable projection over committed truth, never the source of
truth. The engine writes ONLY its own custom fields and adds ONLY items already carrying the engine
label; it never touches Status, column, card position, or any existing item's placement. Every failure
no-ops and discloses — losing the board loses nothing authoritative.

Importing the package does no filesystem or network work; all reads/writes happen inside the called
functions, so the import itself cannot fail or act on a live session. The sole entry the wired hook
runs is ``projects_sync.py session-start`` (a debounced, best-effort, fail-open SessionStart sweep);
the operator-typed setup skill drives ``resolve``/``check`` once a board exists. stdlib-only.
"""
