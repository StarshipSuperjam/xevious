"""export.py — write a saved conversation, or the results of a search, to a readable file.

WHAT THIS IS FOR. Memory is deliberately private and deliberately awkward to get at in bulk: it is gitignored,
it is read through one narrow seam, and nothing prints it wholesale. That is right for the everyday case and
wrong for the occasional one — taking a conversation to someone else, keeping a record of a decision outside
this project, or simply reading a long session somewhere other than a chat window. This is the deliberate,
operator-asked way to do that.

THE DESTINATION GUARD IS THE POINT OF THIS MODULE, NOT A DETAIL. An earlier draft of the window reader carried
a verb that printed verbatim conversation to stdout, and it was cut before it ever shipped — `recall.main` still
records why: "a surface nothing asked for, on the one path where a stray invocation (or a CI log) leaks the
operator's own words." Writing to a file is the same surface with a longer fuse, because the output persists
and this repository's own actor model is that an AI session runs the commits. Only `.engine/memory/` is
gitignored, and every engine tool runs with its working directory inside `.engine/`, so an unguarded relative
default would drop transcripts exactly where a later `git add -A` would sweep them up. So: a destination inside
the working tree is refused unless git itself says the path is ignored, and the refusal explains itself rather
than silently choosing somewhere else.

WHAT AN EXPORT CARRIES WITH IT. The same caveats that ride every read — that this is what was captured rather
than a transcript anyone curated, that long messages were stored in pieces, and that secret-shaped text was
masked only for what was captured after masking existed. A file outlives the session that made it and will be
read by someone with none of that context, so the caveats travel in the file rather than in the chat.

BOUNDED. A whole session can be tens of thousands of records; an unbounded search can be most of the store.
Both are capped, and the cap says plainly in the output that it was reached.
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import index, recall  # noqa: E402

# The most records one export writes. Generous — an export is a deliberate act, not a hot path — but finite,
# so a mistyped query cannot write the whole store to disk in one go.
MAX_RECORDS = 5000

_CAVEATS = (
    "This is the conversation as it was captured, not a transcript anyone edited. Long messages were stored "
    "in pieces and are rejoined here. Text shaped like a password or a key was masked on the way in, but only "
    "for what was captured after that masking existed — read it as private material."
)


class ExportRefused(ValueError):
    """The export did not happen, with the plain-language reason. Raised rather than returned so a caller
    cannot report a file that was never written."""


def _nearest_existing_dir(dest: str) -> str:
    """The closest existing ancestor directory of `dest`.

    Every git question below has to be asked from a directory that EXISTS, and an export's destination very
    often does not yet — `exports/tuesday.md` is the shape an operator actually types. Asking from the missing
    directory made `subprocess.run` raise, which the old code read as "not in a git project" and therefore as
    safe. That inverted the guard precisely on the friendliest path: writing to a brand-new folder inside the
    project was allowed while writing beside it was refused. Walking up first asks the same question about the
    same place, whether or not the leaf exists yet."""
    current = os.path.dirname(os.path.abspath(dest)) or os.sep
    while not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:            # reached the root without finding one
            return os.sep
        current = parent
    return current


def _git_ignores(dest: str, cwd: str) -> bool:
    """True when git itself says `dest` is ignored. Asking git rather than reading `.gitignore` is what makes
    this honest: the rules compose across files, exclusion patterns can re-include a path, and a hand-rolled
    matcher that got any of that wrong would fail in the permissive direction."""
    try:
        done = subprocess.run(["git", "check-ignore", "-q", dest],
                              cwd=cwd, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False              # cannot ask => treat as NOT ignored, the safe direction
    return done.returncode == 0


def _worktree_root(cwd: str) -> "tuple[str | None, bool]":
    """`(root, consulted)` — the git working tree `cwd` sits inside, and whether git could be asked at all.

    The two are reported separately because they mean different things and only one of them is safe to treat
    as "anywhere is fine". `(None, True)` is a real answer: git ran and said this is not a working tree.
    `(None, False)` is the absence of an answer, and an unanswered question must never read as a permission."""
    try:
        done = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              cwd=cwd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None, False
    root = done.stdout.strip()
    return (root if done.returncode == 0 and root else None), True


def assert_safe_destination(dest: str) -> None:
    """Refuse a destination that would put verbatim conversation somewhere a commit could pick it up.

    Outside a git working tree, anywhere is fine — that is the operator's own disk. Inside one, the path must
    be ignored by git. The failure direction is REFUSE, and that now holds for every arm: an unanswerable
    question is refused rather than read as permission, because the cost of being wrong is asymmetric — a
    refused export is an inconvenience, a committed transcript is not retractable."""
    if not isinstance(dest, str) or not dest.strip():
        raise ExportRefused("no destination was given.")
    # REALPATH, not abspath: a destination that is itself a symlink pointing into a working tree was judged
    # by its parent — outside the tree, therefore allowed — and the write then followed the link inside it.
    # Resolving first judges the place the bytes actually land.
    resolved = os.path.realpath(os.path.abspath(dest))
    asked_from = _nearest_existing_dir(resolved)
    root, consulted = _worktree_root(asked_from)
    if not consulted:
        raise ExportRefused(
            "git could not be consulted, so there is no way to tell whether that path sits inside a project "
            "that would commit it. Nothing was written. Choose a destination well outside any project — your "
            "home directory, or a temporary folder."
        )
    if root is None:
        return
    if _git_ignores(resolved, asked_from):
        return
    raise ExportRefused(
        f"that path is inside a git project ({root}) and is not ignored by it, so the export could end up "
        "committed. Nothing was written. Choose somewhere outside the project — your home directory or a "
        "temporary folder — or a path the project already ignores."
    )


def _render_turn(record) -> str:
    speaker = record.get("speaker")
    who = {"user": "**Operator**", "assistant": "**Assistant**"}.get(speaker, f"**{speaker or 'unknown'}**")
    return f"{who}\n\n{record.get('text') or ''}\n"


def session_markdown(session_id: str, *, path: "str | None" = None) -> str:
    """One session's conversation as markdown, rejoined and in order. Reads through `recall`, so a conversation
    the operator withheld is not exported either — the control means what it says on every path.

    Deliberately NOT `recall.window`, whose 200-turn ceiling exists to stop a huge session flooding a live
    session's context. A file has no context to flood, and here that ceiling would be the wrong kind of safe:
    a quarter of real sessions are longer than it, so "export this conversation" would routinely hand back a
    fraction of one and look complete. This reads the session's turns directly and rejoins the chunks with the
    same helper the window reader uses, bounded by `MAX_RECORDS` and saying so in the file when it bites."""
    turns = recall.session_turns(session_id, path=path)
    total = len(turns)
    joined = recall._join_chunks(turns[:MAX_RECORDS])
    lines = [f"# Saved conversation — {session_id}", "", f"_{_CAVEATS}_", ""]
    if not joined:
        lines.append("_Nothing to export: this conversation is not in memory, or you have withheld it._")
        return "\n".join(lines) + "\n"
    lines.append(f"{len(joined)} message{'' if len(joined) == 1 else 's'}.")
    if total > MAX_RECORDS:
        lines.append(f"_Stopped at {MAX_RECORDS} stored records; the conversation continues beyond what is "
                     "here._")
    lines.append("")
    for turn in joined:
        lines.append(_render_turn(turn))
    return "\n".join(lines) + "\n"


def search_markdown(query: str, *, session: "str | None" = None, limit: int = 100,
                    path: "str | None" = None) -> str:
    """The results of one search as markdown, best first. Uses the same seam recall uses, so what lands in the
    file is exactly what a search would have shown — never a wider read done quietly for the export's sake."""
    capped = min(limit if isinstance(limit, int) and limit > 0 else 100, MAX_RECORDS)
    result = index.search(query, session=session, limit=capped, ledger_file=path)
    scope = f" within {session}" if session else ""
    lines = [f"# Saved memory — search for “{query}”{scope}", "", f"_{_CAVEATS}_", ""]
    if not result.records:
        lines.append("_No saved memory matched. Every word of a search has to appear, so a narrower phrase "
                     "finds less, not more._")
        return "\n".join(lines) + "\n"
    lines.append(f"{len(result.records)} result{'' if len(result.records) == 1 else 's'}"
                 + (" (answered from the slower full read)." if result.degraded else "."))
    lines.append("")
    for record in result.records:
        when = record.get("ts")
        stamp = time.strftime("%Y-%m-%d", time.localtime(when)) if isinstance(when, int) else "unknown date"
        sid = record.get("session_id") or "—"
        lines.append(f"## {stamp} · {record.get('role') or record.get('kind') or 'note'} · {sid}")
        lines.append("")
        lines.append(str(record.get("text") or ""))
        lines.append("")
    return "\n".join(lines) + "\n"


def write(text: str, dest: str) -> str:
    """Write `text` to `dest` after the destination guard clears it. Returns the resolved path."""
    assert_safe_destination(dest)
    resolved = os.path.realpath(os.path.abspath(dest))
    parent = os.path.dirname(resolved)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(resolved, "w", encoding="utf-8") as fh:
        fh.write(text)
    return resolved


def main(argv: list) -> int:
    parser = argparse.ArgumentParser(prog="export.py",
                                     description="Write a saved conversation, or a search, to a readable file.")
    sub = parser.add_subparsers(dest="cmd")
    ses = sub.add_parser("session", help="export one whole conversation")
    ses.add_argument("session_id")
    ses.add_argument("dest", help="where to write it (outside the project, or a path git ignores)")
    found = sub.add_parser("search", help="export the results of a search")
    found.add_argument("query")
    found.add_argument("dest")
    found.add_argument("--session", default=None, help="narrow to one conversation")
    found.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "session":
            written = write(session_markdown(args.session_id), args.dest)
        elif args.cmd == "search":
            written = write(search_markdown(args.query, session=args.session, limit=args.limit), args.dest)
        else:
            parser.print_help()
            return 2
    except ExportRefused as exc:
        print(f"Not exported: {exc}")
        return 1
    print(f"Written to {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
