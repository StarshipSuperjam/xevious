"""erase.py — permanent erasure, when the operator asks for it.

THE ONE IRREVERSIBLE ACT, AND WHO PERFORMS IT. Three parts carry it and this is the first: this module writes a
content-free proposal and opens a single-purpose pull request labelled `engine-erasure`. The operator merges it.
A later session's OBSERVER (`erasure_observer`) turns that merge into a gated marker naming the merge commit, and
the next compaction (`compact`) removes the bytes. Nothing here mints a marker and nothing here deletes anything
— a build invariant in `test_forget.py` source-scans for exactly that.

WHY THIS IS ASKED FOR RATHER THAN OFFERED. There used to be an automatic version: a probe scored the store's
already-retired notes, decided which had "earned" erasure, and opened a pull request without anyone asking. It
went with the curation lifecycle it was built on — its one remaining input class was produced by the summary
writer, so after that deletion it would have proposed nothing forever. What replaced it is the shape the
capability always wanted: you name what to erase, and the engine prepares the consent artifact.

THE TERMINAL GATE, AND WHY IT IS THE REAL ONE. This verb refuses unless it is attached to a real terminal, prints
the target's own words, and waits for you to type the confirmation. That is not politeness — it is the barrier.
An AI session's shell has no controlling terminal, so no model path reaches the confirmation, and this repository
carries a blanket permission grant for `uv run` that would otherwise have made "command-line only" worth nothing.
Stated limit, because it matters: this was verified for one runtime on one machine. Another runtime may differ,
and a determined caller could wrap a pseudo-terminal. It is a strong barrier, not a proof.

WHAT THE MERGE GATE IS AND IS NOT. In a repository the engine provisions for a TEAM, the merge requires an
approving review the pull request's author cannot give themselves, and it genuinely prevents. In SOLO — the
shipped default, and this repository — zero approvals are required, so the same actor that opens an erasure pull
request can merge it. There the merge is a VISIBILITY gate: it makes the act a reviewable, attributable commit
that cannot happen quietly. Do not read it as prevention in solo, and do not let the terminal gate be described
as merely advisory: in solo it is the barrier that keeps this out of a model's reach.

THE PROPOSAL STAYS CONTENT-FREE. What is committed is `{"targets": [id, …], "costs": [plain line, …]}` — record
ids and coarse descriptions, never the wording. The wording is shown to you HERE, locally, on your own terminal,
which is the only place it can be shown without putting it somewhere it does not belong. That means the pull
request you merge cannot show you what you are erasing; this verb is where you check that, before it opens.

WITHHOLD FIRST, AND WHAT THAT IS WORTH. A target must already be withheld from recall before it can be proposed
for erasure, so the reversible act always precedes the irreversible one. Be honest about its strength: it is
checked HERE and nowhere else — neither the observer nor the enactment core re-checks it. It is a guard against a
mistake, not a gate against an attacker.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from memory import erasure_observer as observer  # noqa: E402 — reuse its path/label/predicates
from memory import forget, ledger, records       # noqa: E402

# How many of the named records the preview prints in full, and how much of each. A whole conversation can be
# hundreds of turns; the operator needs enough to recognise what they named, not a wall that buries the prompt.
_PREVIEW_MAX = 12
_PREVIEW_CHARS = 200
# The word that must be typed. A whole word rather than "y": this is the one act with no undo, and the cost of
# typing eight characters is the point.
_CONFIRM_WORD = "erase"
_NO_TERMINAL = ("erasing is only ever done from a real terminal, by a person reading what they are erasing. "
                "This was not run from one, so nothing was proposed. Run it yourself in a shell if you meant "
                "it.")
_DAY = 86400
# Plain words for the summary labels an older engine stamped on what it wrote, so a line about one of those
# records never surfaces a raw engine token. Nothing writes them any more, but a store that has been running is
# full of them, and they are exactly the kind of thing an operator asks to have erased. Anything else degrades
# to the neutral "a note".
_ROLE_PHRASE = {
    "decision": "a decision",
    "rationale/pushback": "a note about why a choice was made",
    "lesson": "a lesson",
    "dead-end": "a note about an approach that was set aside",
    "preference": "a preference",
    "intent": "a plan",
    "observation": "an observation",
}


def _role_phrase(role) -> str:
    return _ROLE_PHRASE.get(role, "a note")


def _age_phrase(seconds: int) -> str:
    """A coarse, content-free age bucket (no raw timestamp). Grammatical across the range the operator may vary into."""
    days = max(0, int(seconds // _DAY))
    if days < 14:
        return "in the last couple of weeks"
    if days < 31:
        return "a few weeks ago"
    if days < 75:
        return "about a month ago"
    if days < 320:
        return f"about {round(days / 30)} months ago"
    if days < 550:
        return "about a year ago"
    return "over a year ago"


def _cost_for(record: dict, now: int) -> str:
    """The content-free line describing ONE named record in the committed proposal — built ONLY from what it is
    and roughly when, never its `text`, `session_id` or `tags`.

    It says what KIND of thing this is, not what it says, and that is a deliberate limit rather than an
    oversight: the proposal is committed to a branch and read on a pull-request page, and neither is a place
    for the operator's own words. Checking that these are the right records happens on their own terminal
    before this file is ever written (`preview`)."""
    ts = record.get("ts")
    age = now - ts if isinstance(ts, int) and not isinstance(ts, bool) else 0
    if record.get("kind") == records.AMBIENT_CAPTURE_KIND:
        speaker = record.get("speaker")
        whose = {"user": "something you said", "assistant": "something the assistant said"}.get(
            speaker, "a message")
        return (f"{whose} in a saved conversation — {_age_phrase(age)}; you withheld it from recall already, "
                "and it stays fully recoverable until this is merged.")
    if record.get("kind") == records.PIN_KIND:
        return (f"a note you asked to be remembered — {_age_phrase(age)}; withheld from recall already, and "
                "fully recoverable until this is merged.")
    return (f"{_role_phrase(record.get('role'))} — {_age_phrase(age)}; withheld from recall already, and "
            "fully recoverable until this is merged.")


def build_proposal(records_in: list, *, now: "int | None" = None) -> dict:
    """The committed batch proposal `{"targets": [id, …], "costs": [line, …]}` for one or more earned notes —
    EXACTLY those two keys, both content-free, and `costs[i]` describes `targets[i]` (parallel, one-to-one). Each
    `target` is validated to the observer's record-id shape; each `cost` is plain language from the note's role + a
    coarse age bucket (never the note's text/session/tags). Raises on an empty list or any record without a
    valid content-free id (so an invalid target can never enter the grammar)."""
    if not records_in:
        raise ValueError("refusing to build a proposal with no targets")
    now = int(time.time()) if now is None else now
    targets: list = []
    costs: list = []
    for record in records_in:
        target = record.get(records.RECORD_ID_KEY)
        if not observer._is_record_id(target):
            raise ValueError("refusing to build a proposal for a record without a content-free id")
        targets.append(target)
        costs.append(_cost_for(record, now))
    return {"targets": targets, "costs": costs}


def write_proposal(proposal: dict, *, root: "str | None" = None) -> str:
    """Write the batch proposal to the observer's fixed committed path under `root` (the repo root by default; a
    throwaway root in tests/demo). Refuses to write a proposal whose `targets` are not ALL content-free record ids,
    that is empty, or whose `costs` do not correspond one-to-one to its targets (so an invalid or mismatched batch
    can never land — the operator must read a cost line for exactly the notes that will be erased). Overwrites in
    place — there is only ever one canonical proposal. Returns the path written."""
    targets = proposal.get("targets")
    costs = proposal.get("costs")
    if not isinstance(targets, list) or not targets or not all(observer._is_record_id(t) for t in targets):
        raise ValueError("refusing to write a proposal whose targets are not all content-free record ids")
    if not isinstance(costs, list) or len(costs) != len(targets):
        raise ValueError("refusing to write a proposal whose costs do not correspond one-to-one to its targets")
    if root is None:
        import validate  # lazy: only the real write needs the repo root
        root = validate.ROOT
    dest = os.path.join(root, observer._PROPOSAL_PATH)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump({"targets": targets, "costs": [str(c) for c in costs]}, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return dest



def _reader(transport=None):
    """A GitHub boundary over the operator's `gh` token (label = `engine-erasure`), or None when the repo/token can't
    be resolved (a degraded host). Tests/demo inject a `transport` (the same 2-tuple seam the observer uses), so the
    real logic runs fully offline. Lazy-imported so the cold-start load stays light."""
    import telemetry  # noqa: E402 — its GitHubIssues exposes the injectable 2-tuple transport + ensure_label
    if transport is not None:
        return telemetry.GitHubIssues("local/practice", "practice-token",
                                      label=observer.ERASURE_LABEL, transport=transport)
    import boot  # noqa: E402 — lazy: keep boot's heavy import graph off the module-load path
    repo = boot.repo_slug()
    token = boot.gh_token()
    if not repo or not token:
        return None
    return telemetry.GitHubIssues(repo, token, label=observer.ERASURE_LABEL)


def _apply_label(gh, number: int) -> bool:
    """Ensure the `engine-erasure` label exists — with ITS OWN colour and description (never the engine label's grey
    'health' identity `gh.ensure_label()` would stamp, since `gh.label` is `engine-erasure` here) — and apply it to the
    just-opened pull request (a PR is an issue for labelling). This is the producer-side self-heal for a repo that never
    ran first-run provisioning (this genesis repo); provisioning creates the same label from the same canonical trio.
    The observer discovers ONLY by this label, so this is load-bearing. Fail-OPEN: a label failure leaves the PR
    un-discovered (safe — no erasure) rather than raising; returns True iff the label was applied."""
    try:
        gh.ensure_named_label(observer.ERASURE_LABEL, observer.ERASURE_LABEL_COLOR, observer.ERASURE_LABEL_DESCRIPTION)
        gh._transport("POST", f"/repos/{gh.repo}/issues/{number}/labels", {"labels": [observer.ERASURE_LABEL]})
        return True
    except Exception:  # noqa: BLE001 — a degraded host must not strand the caller; the un-labelled PR simply won't fire
        return False


# --- the real PR opener (INJECTED in tests/demo; NEVER runs in the construction repo) ----------------------

def _open_erasure_pr(gh, branch: str, title: str, body: str, content: str):
    """THE GIT+PR BOUNDARY — HOOK-SAFE: build the branch, commit the single proposal file, and open the single-purpose
    pull request ENTIRELY via the GitHub API over the bounded `gh` transport (create-ref -> put-contents -> open-pull).
    There is NO local git and NO working-tree mutation, and every call is timeout-bounded — so the background
    SessionStart trigger can never switch the operator's branch out from under a live session, nor hang on a stalled
    `git push`. The PUT commits EXACTLY the one proposal file, so the merge tree the observer reads carries exactly that
    one change (single-purpose). Fail-SAFE throughout: any non-success status, unreadable body, or transport fault ->
    return None (the caller reports a retry, never a raise from a hook). A pre-existing branch ref (422) is a STALE
    leftover of a closed/merged erasure PR (the in-flight serializer guarantees no OPEN one when the opener runs), so
    re-offer replaces it — but only after VERIFYING (never inferring) it backs no open pull request, so a deterministic
    branch name still can never duplicate. Returns the new pull-request number, or None.

    THE DUPLICATE PROTECTION IS THE RE-VERIFY BELOW, and nothing else. The automatic proposer paired this with
    an in-flight serializer that swept open erasure pull requests before opening another; that ran because a
    hook fired it unasked, and it is gone with it. An operator asking twice is not a race, so what remains is
    the check that matters: a stale branch ref is only replaced after CONFIRMING no open pull request is
    backed by it."""
    import boot  # noqa: E402 — lazy: only for the protected-branch name
    base = getattr(boot, "PROTECTED_BRANCH", "main")
    try:
        head = observer._get(gh, f"/repos/{gh.repo}/git/ref/heads/{base}")
        base_sha = (head or {}).get("object", {}).get("sha")
        if not isinstance(base_sha, str) or not base_sha:
            return None
        status, _ = gh._transport("POST", f"/repos/{gh.repo}/git/refs",
                                  {"ref": f"refs/heads/{branch}", "sha": base_sha})
        if status == 422:                            # branch already exists -> a stale leftover; replace it for re-offer
            # VERIFY it backs no OPEN pull request before deleting its head — a label POST can fail-open, leaving an
            # open-but-unlabelled PR the label-filtered serializer cannot see; orphaning its head would let a duplicate
            # open. Unreadable / malformed / any live PR -> DECLINE (never delete on doubt, never duplicate).
            owner = gh.repo.split("/")[0]
            backing = observer._get(gh, f"/repos/{gh.repo}/pulls?head={owner}:{branch}&state=open")
            if not isinstance(backing, list) or backing:
                return None
            del_status, _ = gh._transport("DELETE", f"/repos/{gh.repo}/git/refs/heads/{branch}", None)
            if del_status not in (200, 204):         # a successful ref delete is 204 No Content
                return None
            status, _ = gh._transport("POST", f"/repos/{gh.repo}/git/refs",
                                      {"ref": f"refs/heads/{branch}", "sha": base_sha})
        if status not in (200, 201):                 # a non-422 error, or a re-create that still failed -> decline
            return None
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        put = {"message": title, "content": encoded, "branch": branch}
        # proposal.json ships as a committed placeholder, so it already exists on the branch — the Contents API needs
        # the existing blob sha to UPDATE it (omitting it 422s). A fresh tree without the file -> no sha -> create.
        existing = observer._get(gh, f"/repos/{gh.repo}/contents/{observer._PROPOSAL_PATH}?ref={base}")
        file_sha = (existing or {}).get("sha")
        if isinstance(file_sha, str) and file_sha:
            put["sha"] = file_sha
        status, _ = gh._transport("PUT", f"/repos/{gh.repo}/contents/{observer._PROPOSAL_PATH}", put)
        if status not in (200, 201):
            return None
        status, pr = gh._transport("POST", f"/repos/{gh.repo}/pulls",
                                   {"title": title, "head": branch, "base": base, "body": body})
        if status not in (200, 201) or not isinstance(pr, dict):
            return None
        number = pr.get("number")
        return number if isinstance(number, int) else None
    except Exception:  # noqa: BLE001 — fail-SAFE: a degraded host yields no open, never a raise from a SessionStart hook
        return None


def _branch_for(targets: list) -> str:
    """A deterministic branch name over the whole target SET (order-independent), so a byte-identical batch maps to a
    stable branch. On a re-offer of the same set, `_open_erasure_pr` finds the stale ref (422) and — after verifying
    it backs no open PR — replaces it, so re-offer is not blocked by the leftover. The real one-at-a-time protection
    is the in-flight serializer (`_open_erasure_pr_numbers`), not this name."""
    digest = hashlib.sha1("\n".join(sorted(targets)).encode("utf-8")).hexdigest()
    return f"erasure-{digest[:12]}"


def _pr_title(n: int) -> str:
    return f"Erase {n} remembered note{'' if n == 1 else 's'} (single-purpose)"


def _collapse(costs: list) -> list:
    """Collapse identical cost lines into "{k} notes — {line}" rows (ordered by first appearance), each with its
    count stated explicitly (never a bare total). Lines vary by role + age, so in practice a crash-duplicate batch
    stays one line per note (count 1) and the per-note enumeration is preserved; the collapse is what keeps a
    batch of near-identical lines legible if one ever arises." Reads ONLY the content-free `costs` — no session id / record id / text — so the committed
    `targets`/`costs` stay 1:1 and no grouping key ever leaks into the rendered body."""
    order: list = []
    counts: dict = {}
    for c in costs:
        if c not in counts:
            order.append(c)
        counts[c] = counts.get(c, 0) + 1
    return [f"- {c}" if counts[c] == 1 else f"- {counts[c]} notes — {c}" for c in order]


def _pr_body(proposal: dict) -> str:
    """The pull-request body the operator merges — plain language, no engine jargon, and no record content.

    THIS IS THE CONSENT SURFACE, AND IT IS DELIBERATELY THIN. It says how many records and what kind of thing
    each is; it cannot show the wording, because the wording would then live on a branch and a pull-request
    page. That is why the terminal verb shows it first and takes a typed confirmation there: by the time this
    body exists, the operator has already read what they named. Merging is consent to erase exactly the ids
    committed alongside it — `write_proposal` pins the lines one-to-one with those ids, so the list read here
    is the list a later session erases.

    All-or-nothing, and it says so: merge erases every record listed, closing keeps every one of them and
    changes nothing. Raises on an empty batch (no caller reaches it with none)."""
    costs = proposal.get("costs") or []
    n = len(costs)
    if n == 0:
        raise ValueError("refusing to render a consent body for a proposal with no records")
    asked = ("You asked the engine to erase this, from a terminal, after reading it back. This pull request is "
             "the record of that request, and merging it is what carries it out.")
    if n == 1:
        return (
            "This pull request permanently erases **one record** from the engine's memory.\n\n"
            f"**What it is:** {costs[0]}\n\n"
            f"{asked} Nothing is erased the moment you merge: a later session carries it out. This is the one "
            "thing the engine can do to its memory that cannot be undone.\n\n"
            "Changed your mind? **Close** this pull request. Nothing is erased, and the record stays exactly "
            "where it is — withheld from recall, and fully recoverable.\n")
    listed = "\n".join(_collapse(costs))
    return (
        f"This pull request permanently erases **{n} records** from the engine's memory.\n\n"
        f"**What they are** (identical entries are grouped with a count):\n\n"
        f"{listed}\n\n"
        f"{asked} Nothing is erased the moment you merge: a later session carries it out. This is the one thing "
        "the engine can do to its memory that cannot be undone.\n\n"
        f"**All or nothing.** Merging erases all {n}; closing keeps all {n}. There is no way to keep some — if "
        "you want even one of them back, close this and ask again for a narrower set. Nothing is erased by "
        "closing, and every record stays withheld from recall and fully recoverable.\n")


# --- what the operator is about to erase -------------------------------------------------------------------

class EraseRefused(RuntimeError):
    """The erasure was not proposed, carrying the plain-language reason. Raised rather than returned so no
    caller can report a proposal that was never written."""


def _targets_for(name: str, *, path: "str | None" = None) -> list:
    """Every live record the operator named — one record by its id, or a whole conversation by its session id.

    Reads the RAW ledger rather than `forget.live_records`, and deliberately: every legal target is already
    withheld, and `live_records` is precisely the stream a withhold removes a record from. Reading through it
    would find nothing and report that the operator's own conversation does not exist."""
    src = path or ledger.ledger_path()
    out = []
    for record in ledger.iter_records(path=src):
        if not isinstance(record, dict):
            continue
        if record.get(records.RECORD_ID_KEY) == name or record.get("session_id") == name:
            out.append(record)
    return out


def _refuse_unless_withheld(targets: list, *, path: "str | None" = None) -> None:
    """Refuse a target the operator has not already taken out of recall.

    The reversible act comes first. Its worth is bounded and stated in the module docstring: nothing downstream
    re-checks this, so it stops a mistake rather than an attacker."""
    src = path or ledger.ledger_path()
    ids, sessions = forget.withheld_targets(src)
    for record in targets:
        if not forget.is_withheld(record, ids, sessions):
            raise EraseRefused(
                "that is still part of your recallable memory. Erasure is permanent, so it only ever follows "
                "the reversible step: withhold it first, live without it for as long as you like, and ask "
                "again if you still want it gone."
            )


def preview(targets: list) -> str:
    """The operator's own words, for reading on their own terminal before they confirm.

    This is the ONLY surface that can show them. The committed proposal is content-free by design, so the pull
    request they merge names ids and coarse descriptions and nothing else — which means the check that the ids
    are the ones they meant has to happen here or nowhere."""
    lines = [f"About to propose erasing {len(targets)} record{'' if len(targets) == 1 else 's'}.", ""]
    for record in targets[:_PREVIEW_MAX]:
        when = record.get("ts")
        stamp = time.strftime("%Y-%m-%d", time.localtime(when)) if isinstance(when, int) else "unknown date"
        speaker = record.get("speaker") or record.get("role") or record.get("kind") or "record"
        text = " ".join(str(record.get("text") or "").split())
        if len(text) > _PREVIEW_CHARS:
            text = text[:_PREVIEW_CHARS].rstrip() + "…"
        lines.append(f"  [{stamp} · {speaker}] {text or '(no text)'}")
    if len(targets) > _PREVIEW_MAX:
        lines.append(f"  … and {len(targets) - _PREVIEW_MAX} more.")
    lines.append("")
    return "\n".join(lines)


def _confirmed_on_a_terminal(targets: list, *, stream=None) -> bool:
    """Show the operator what they are erasing and wait for them to type the confirmation.

    REFUSES WITHOUT A CONTROLLING TERMINAL, and that refusal is the barrier — see the module docstring. An
    automated caller has no terminal, so it never reaches the prompt; a person running this in their own shell
    always does."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise EraseRefused(_NO_TERMINAL)
    out = stream or sys.stdout
    out.write(preview(targets))
    out.write(f"This opens a pull request. Merging it erases the above permanently — there is no undo.\n"
              f"Type {_CONFIRM_WORD!r} to propose it, or anything else to stop: ")
    out.flush()
    return sys.stdin.readline().strip() == _CONFIRM_WORD


def request(name: str, *, path: "str | None" = None, opener=None, transport=None,
            root: "str | None" = None, stream=None) -> dict:
    """Propose erasing everything the operator named. Returns a small report dict.

    The whole order matters: resolve the target, refuse unless it is already withheld, show the wording and take
    the typed confirmation, and only then write the proposal and open the pull request. Every refusal happens
    before anything is written or opened."""
    # THE TERMINAL CHECK COMES FIRST, before any question about the store is answered. Ordered the other way,
    # an automated caller with no terminal could still learn which record and session ids exist and which are
    # withheld, one refusal message at a time — a small leak, but the gate should be the first thing.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise EraseRefused(_NO_TERMINAL)
    targets = _targets_for(name, path=path)
    if not targets:
        raise EraseRefused(
            f"nothing in your memory has the identifier {name!r}, so there is nothing to erase. Nothing was "
            "written. `list-withheld` names what you have taken out of recall, which is what can be erased."
        )
    _refuse_unless_withheld(targets, path=path)
    if not _confirmed_on_a_terminal(targets, stream=stream):
        return {"status": "declined", "proposed": 0,
                "message": "Nothing was proposed and nothing was changed."}
    proposal = build_proposal(targets)
    gh = _reader(transport=transport)
    if gh is None:
        write_proposal(proposal, root=root)
        return {"status": "written", "proposed": len(targets),
                "message": "The proposal was written, but the engine could not reach GitHub to open the pull "
                           "request. Nothing is erased until one is merged."}
    open_pr = opener or _open_erasure_pr
    number = open_pr(gh, _branch_for(proposal["targets"]), _pr_title(len(targets)),
                     _pr_body(proposal), json.dumps(proposal, indent=2, ensure_ascii=False) + "\n")
    if number is None:
        return {"status": "retry", "proposed": 0,
                "message": "The pull request could not be opened just now. Nothing was erased; try again."}
    _apply_label(gh, number)
    return {"status": "proposed", "proposed": len(targets), "pr": number,
            "message": f"Opened pull request #{number}. Nothing is erased until you merge it."}


def main(argv: list) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: erase.py <record-id|session-id>\n\n"
              "Proposes permanently erasing what you name, by opening a pull request you then merge.\n"
              "The target must already be withheld from recall. Must be run from a real terminal.")
        return 0 if argv else 2
    try:
        report = request(argv[0])
    except EraseRefused as exc:
        print(f"Not proposed: {exc}")
        return 1
    print(report["message"])
    return 0 if report["status"] in ("proposed", "written", "declined") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
