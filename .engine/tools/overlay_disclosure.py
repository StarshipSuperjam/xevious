#!/usr/bin/env python3
"""engine-overlay-disclosure — a NON-BLOCKING, merge-time notice that a pull request changes engine files
the next engine update will OVERWRITE, so a deployed operator learns, at the change, that the edit won't
survive the next update. It posts one plain-language comment on the pull request (the surface a
non-engineer actually reads — a soft check would land only in the Actions log). It NEVER blocks a merge.

This is DISTINCT from the guardrail-weakening acknowledgment (`guardrail-ack` / `engine-guard`): that guards
*weakening a protection* (a deliberate, blocking consent act); this discloses *a change that won't survive
an update* (a routine heads-up). The two carry different consent weight and stay separate — see eADR-0037.

Deployed-only. It discloses only when this repo has an update HOME that is a DIFFERENT repo than its own
origin (an upstream that will overlay it). In the self-hosting engine repo — which IS its own home — there is
no upstream to overwrite it, so it is silent (this is why it never fires on engine-template's own pull
requests). It also stays silent on an engine-authored lifecycle pull request (the update / arrival / removal
PRs), whose whole point is to bring or remove that content — warning there would read backwards.

Same-repo pull requests only: on a fork pull request it stays quiet (the read-only fork token cannot post,
and the disclosure covers the operator's own changes), which also removes the fork-write-token attack
surface — this is why the workflow uses `pull_request`, not `pull_request_target`.

The overwrite set is `module_manager.overlay_replace_paths()` — the SAME enumeration the real update overlay
copies (via the shared `_overlay_copy_map`), so the notice cannot drift from the overlay's own LOGIC for what
it overwrites. It is not, and does not claim to be, a guarantee about a future release's exact file set: the
live tree stands in for that release (an honest approximation — a path a future release adds or drops is
inherent slack), and it never warns about a file the update PRESERVES (operator config, the keyed-merge
fences of CLAUDE.md/AGENTS.md/.gitignore, the per-deployment eADR stream).

RENDER SAFETY: a rename can place an attacker-chosen filename into the tree (and so into the overwrite set),
so every rendered path is passed through `_safe_path`, which drops any character outside a conservative
file-path whitelist. This keeps the markdown code span airtight — NOT markdown backslash-escaping, which does
not work inside a code span (CommonMark). So no crafted filename can inject a link or markup into a comment a
non-engineer is trained to trust.

The pieces are drawn from the closest existing precedents: the changed-files read is `weakening_guard`'s; the
plain-language PR comment is `release_terminal`'s posture; the invisible dedup marker is
`issue_conformance_ci`'s (tightened here to only reconcile a BOT-authored marker comment, so a user comment
that happens to quote the marker is never overwritten). The "always inform, never block, but a FAILURE must
be visible" exit contract is a deliberate divergence from `issue_conformance_ci` (which reds on write failure
by design): a clean run and a BROKEN run must look different, or "no comment" would falsely reassure the
operator that nothing will be overwritten.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

import boot
import github_client
import module_manager
import repo_identity
import validate
import weakening_guard

COMMENT_MARKER = "<!-- engine-overlay-disclosure -->"
USER_AGENT = "engine-overlay-disclosure"

# Engine-authored lifecycle pull requests bring or remove the engine's own content; the notice would read
# backwards on them (their whole point is to replace/remove these files), so they are exempted. The branches
# are deterministic: module_manager opens `engine-update-<ref>` (the ref varies) and `engine-remove`;
# instantiator opens `engine-arrival`. The two fixed names are matched EXACTLY (so an operator branch like
# `engine-remove-cleanup` is not wrongly exempted); only the varying-ref update branch is matched by prefix.
_ENGINE_AUTHORED_BRANCH_EXACT = ("engine-remove", "engine-arrival")
_ENGINE_AUTHORED_BRANCH_PREFIXES = ("engine-update-",)

# A conservative file-path whitelist. Real engine paths use only these, so the substitution is lossless for
# them; anything else (a crafted rename target) becomes '?', which cannot break a markdown code span.
_UNSAFE_PATH_CHAR = re.compile(r"[^A-Za-z0-9._/-]")

_MAX_LISTED = 15   # cap the rendered list so a large edit is not a wall of paths; the rest is summarized.


class _CommentError(Exception):
    """A GitHub read/write failure. Surfaced as a VISIBLE (non-blocking) red — never swallowed into a green
    that would read as 'nothing to overwrite'."""


class _Comments:
    """The thin GitHub comments client (a pull request is an Issue for the comments API). Mirrors
    `issue_conformance_ci`'s injectable-transport shape so tests/the demo pass a fake transport."""

    def __init__(self, repo: str, token: str, *, transport=None):
        self.repo = repo
        self.token = token
        self._transport = transport or self._http

    def _http(self, method: str, path: str, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = github_client.request(path, self.token, user_agent=USER_AGENT, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as exc:            # 4xx/5xx — surface the status, never swallow
            return exc.code, None
        except urllib.error.URLError as exc:             # network unreachable — a real failure
            raise _CommentError(f"GitHub is unreachable: {exc}") from exc

    def list_comments(self, number: int) -> list:
        out, page = [], 1
        while True:
            status, data = self._transport(
                "GET", f"/repos/{self.repo}/issues/{number}/comments?per_page=100&page={page}", None)
            if status >= 400 or data is None:
                raise _CommentError(f"GitHub returned {status} listing comments on #{number}")
            out.extend(data)
            if len(data) < 100:
                break
            page += 1
        return out

    def post_comment(self, number: int, body: str) -> None:
        status, _ = self._transport("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})
        if status >= 400:
            raise _CommentError(f"GitHub returned {status} commenting on #{number}")

    def edit_comment(self, comment_id, body: str) -> None:
        status, _ = self._transport("PATCH", f"/repos/{self.repo}/issues/comments/{comment_id}", {"body": body})
        if status >= 400:
            raise _CommentError(f"GitHub returned {status} editing comment {comment_id} on {self.repo}")


def is_deployed(root: "str | None" = None) -> bool:
    """True iff this repo has an engine update HOME that is a DIFFERENT repo than its own ON-DISK git origin —
    an upstream that will overlay (overwrite) its engine files on the next update. False (silent) when no home
    is recorded, when the origin can't be read, or when home == origin (the engine's own home repo, which has
    no upstream to overwrite it).

    Both sides come from `repo_identity`, so both describe the SAME checkout: the origin is read from disk,
    never from `GITHUB_REPOSITORY`, and the comparison is slug-normalized. A recorded home differing only in
    case or a trailing `.git` therefore can no longer make the engine's own repo read as deployed and comment
    on its own pull requests — and since `.engine/engine.json` is carved out of the overlay, such a value would
    persist with no update able to correct it.

    A malformed manifest RAISES rather than reading as "not deployed"; `main()` turns that into a visible,
    non-blocking failure. `root` is injectable (default: this process's checkout) so the tests and the demo can
    drive both verdicts against real throwaway checkouts."""
    return repo_identity.is_downstream_copy_strict(root or validate.ROOT)


def _is_bot(comment: dict) -> bool:
    """True iff the comment was authored by a bot/Actions token — so the notice only ever reconciles (edits)
    a comment IT posted, never a user comment that happens to quote the invisible marker."""
    return ((comment.get("user") or {}).get("type")) == "Bot"


def _safe_path(path: str) -> str:
    """A render-safe form of `path`: every character outside a conservative file-path whitelist (letters,
    digits, dot, underscore, slash, hyphen) becomes '?'. Real engine paths use only those, so this is
    lossless for them; it neutralizes a crafted rename target so no backtick can terminate the code span and
    no bracket/paren/angle-bracket/autolink can form inside it. Backslash-escaping is deliberately NOT used —
    it has no effect inside a markdown code span (CommonMark)."""
    return _UNSAFE_PATH_CHAR.sub("?", path)


def overwritten_paths(changed: list) -> list:
    """The engine-canonical paths this pull request changes that the next update would overwrite: the
    intersection of `module_manager.overlay_replace_paths()` with the pull request's changed files, keeping
    only changes that REPLACE an existing file — a pure add is not overwritten (mirrors the guardrail guard's
    status filter). Only paths already in the overwrite set are returned; a rename's crafted new name is
    still rendered safely by `_safe_path` at compose time (the set is globbed from the tree, so a rename
    target present in the tree can be a member — the render sanitizer, not this filter, is the injection
    boundary)."""
    overwrite = module_manager.overlay_replace_paths()
    hits = set()
    for f in changed:
        if f.get("status") not in weakening_guard.WEAKENING_STATUS:
            continue
        for cand in (f.get("filename"), f.get("previous_filename")):
            if cand and cand in overwrite:
                hits.add(cand)
    return sorted(hits)


def compose_comment(paths: list, home: str | None) -> str:
    """The plain-language, non-blocking disclosure body. Peer voice: inform + consequence, route to the
    durable home (named, when known), never forbid. Every path is rendered through `_safe_path`, and a long
    list is capped so it never becomes a wall."""
    shown = [f"- `{_safe_path(p)}`" for p in paths[:_MAX_LISTED]]
    if len(paths) > _MAX_LISTED:
        shown.append(f"- …and {len(paths) - _MAX_LISTED} more")
    listed = "\n".join(shown)
    home_hint = f" (`{_safe_path(home)}`)" if home else ""
    return (
        f"{COMMENT_MARKER}\n"
        "**Heads-up: this pull request changes engine files the next engine update is set to overwrite.**\n\n"
        "The engine keeps its own machinery current by replacing these files wholesale when you update — so a "
        "change you make to them here won't survive the next update; it will quietly revert:\n\n"
        f"{listed}\n\n"
        "If that was a one-off, this is just so you know. If you want the change to last, the durable home "
        f"for an edit to engine machinery is upstream in the engine project these files come from{home_hint} "
        "— a fix there travels to every update. And if what you actually want is to customize how the engine "
        "behaves, the settings that *do* survive an update are your tunable policy (via `/engine-tune`) and "
        "your operator notes — those are preserved; these files are not.\n\n"
        "*This is a heads-up only — it does not block your merge, and your merge is the decision.*"
    )


def _resolved_body() -> str:
    """The retraction body: a prior notice whose files are no longer in the pull request is edited to this,
    never destructively deleted."""
    return (f"{COMMENT_MARKER}\n"
            "*(Resolved — this pull request no longer changes engine files the next update would "
            "overwrite.)*")


def reconcile(client: _Comments, number: int, paths: list, home: str | None) -> str:
    """Post / update / retract the single BOT-authored marker comment idempotently (and resolve any stray
    duplicate). Returns a plain status word."""
    mine = [c for c in client.list_comments(number)
            if COMMENT_MARKER in (c.get("body") or "") and _is_bot(c)]
    if paths:
        body = compose_comment(paths, home)
        if not mine:
            client.post_comment(number, body)
            outcome = "posted"
        elif (mine[0].get("body") or "") != body:
            client.edit_comment(mine[0].get("id"), body)
            outcome = "updated"
        else:
            outcome = "unchanged"
        for extra in mine[1:]:                                 # a duplicate must not linger as a live notice
            if (extra.get("body") or "") != _resolved_body():
                client.edit_comment(extra.get("id"), _resolved_body())
        return outcome
    retracted = False
    for c in mine:
        if (c.get("body") or "") != _resolved_body():
            client.edit_comment(c.get("id"), _resolved_body())
            retracted = True
    return "retracted" if retracted else "clean"


def _load_event() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH", "")
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def _is_fork(event: dict, base_repo: str) -> bool:
    """True when the pull request comes from a fork (head repo != base repo), OR when provenance cannot be
    determined — fail safe: never post with uncertain provenance."""
    pr = event.get("pull_request") or {}
    head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    base = (((pr.get("base") or {}).get("repo") or {}).get("full_name")) or base_repo
    if not head_repo or not base:
        return True
    return head_repo != base


def _is_engine_authored(event: dict) -> bool:
    """True iff the pull request is an engine-authored lifecycle PR (update / removal / arrival), identified
    by its deterministic head branch — the notice is exempt there (that PR brings or removes the content)."""
    head_ref = ((event.get("pull_request") or {}).get("head") or {}).get("ref") or ""
    return head_ref in _ENGINE_AUTHORED_BRANCH_EXACT or head_ref.startswith(_ENGINE_AUTHORED_BRANCH_PREFIXES)


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = boot.gh_token() or ""
    event = _load_event()
    number = (event.get("pull_request") or {}).get("number")

    # Its own try, BEFORE the gates below: a corrupt manifest must not read as a clean "nothing to disclose",
    # which an operator takes as "nothing will be overwritten". Widening the try further down instead would
    # reorder the number / engine-authored / fork gates.
    try:
        deployed = is_deployed()
    except Exception as exc:  # noqa: BLE001 — unreadable identity is a BROKEN run: visible, never a false all-clear
        print(f"overlay-disclosure: could not read this repository's engine record (.engine/engine.json), so "
              f"the heads-up about files a future update would overwrite did not run. This does not block your "
              f"merge. The file looks damaged — restore it from your project's history. ({exc})", file=sys.stderr)
        return 1
    if not deployed:
        print("overlay-disclosure: no distinct update home (self-hosting or unset) — nothing to disclose.")
        return 0
    if number is None:
        print("overlay-disclosure: no pull request in the event — nothing to do.")
        return 0
    if _is_engine_authored(event):
        print("overlay-disclosure: an engine-authored lifecycle pull request brings/removes this content "
              "— nothing to disclose.")
        return 0
    if _is_fork(event, repo):
        print("overlay-disclosure: fork pull request — the disclosure covers same-repo changes only.")
        return 0

    try:
        changed = weakening_guard.fetch_all_changed_files(repo, number, token)
        paths = overwritten_paths(changed)
        status = reconcile(_Comments(repo, token), number, paths, repo_identity.home_repository(validate.ROOT))
    except Exception as exc:  # noqa: BLE001 — a broken disclosure must be VISIBLE (non-blocking), not green
        print(f"overlay-disclosure: could not complete the disclosure ({exc}).", file=sys.stderr)
        return 1

    print(f"overlay-disclosure: {status} "
          f"({len(paths)} file(s) the next update would overwrite).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
