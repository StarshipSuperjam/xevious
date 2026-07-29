"""erasure_observer.py — the cross-session Layer-2 erasure OBSERVER (memory substrate).

This is the memory substrate's single irreversible act: physically erasing a remembered note. The enactment core is
a content-free `operator-adjudicated-erasure` marker, a gated removal inside `compact()`, and
`compact.enact_erasure(target_id, merge_sha)` (the SOLE minter, append-only). THIS module is that minter's one live
caller: at SessionStart it turns a *merged single-purpose erasure pull request* into the marker, so the next
compaction removes the named note.

The consent gate is the locked law: erasure happens ONLY because the operator merged a single-purpose
erasure PR — *the merge event only*; a merely-closed (declined / auto-resolved) Issue or PR NEVER erases. So the
observer:

  - **discovers** candidate erasure PRs by the dedicated `engine-erasure` label ("by label/search"), among
    CLOSED items only;
  - **confirms a genuine merge** — `merged_at` non-null AND a real `merge_commit_sha` — never acting on a close;
  - **binds the target to the IMMUTABLE merge tree** — it reads the content-free target id from a committed proposal
    file at `?ref=merge_commit_sha` (the merge commit's tree is frozen; the PR *body* is post-merge-mutable and is
    NEVER read). It validates the id is exactly the content-free record-id shape and reads NOTHING but that id (no
    gitignored ledger content, no operator-facing cost);
  - **dedups on the target id alone** — the retained marker (the enactment tombstone) is the cross-session dedup ledger, so
    a re-merged PR never re-mints or re-fires the one-time heads-up;
  - **enacts** via `compact.enact_erasure`, and on a NEW enactment relays ONE plain-language heads-up.

Posture: **fail-SAFE on consent, fail-OPEN on host.** Any doubt — no token, unreachable GitHub, unmerged, an
unreadable/malformed proposal, a bad id shape — yields no erasure and a silent proceed (retry next session). The
SessionStart hook can never block or slow the session past one bounded, swallowed read.

Why this is not an AI-reachable note-shredder — stated precisely, because the honest bound is narrower than the
obvious one. The merge SHA comes from a GENUINE merge to protected `main`, never from argv: nothing here can
fabricate `merged_at`/`merge_commit_sha` or forge the committed proposal at the merge tree, and there is
deliberately no arbitrary-mint verb. The label only *discovers*; the binding is the immutable proposal@sha, so a
mislabelled random merged PR (no proposal at its tree) is a no-op.

WHAT THE MERGE ITSELF IS WORTH DEPENDS ON THE TIER, and this used to claim more than it had. In a TEAM
repository the ruleset requires an approving review the author cannot give themselves, and the merge genuinely
prevents. In SOLO — the shipped default — the provisioned ruleset requires zero approving reviews, so the same
actor that opens an erasure pull request can merge it. There the merge is a VISIBILITY gate: it makes the act a
reviewable, attributable commit that cannot happen quietly. What bounds the MODEL path in solo is upstream of
here: `erase.py` refuses unless it is attached to a controlling terminal, which an automated shell does not
have.

Leaf discipline: stdlib + the cycle-free `memory` set (`compact` / `ledger` / `records`) + the sibling `hooks`; the
GitHub reader (`boot` resolvers + `telemetry`'s 2-tuple transport) is lazy-imported inside the network path so the
cold-start load stays light. Tests/demo replace ONLY the injected `_transport` (`gh._transport(method, path, body) ->
(status, json)`) — no live GitHub. Run the demo:
    uv run --directory .engine --frozen -- python tools/memory/erasure_observer.py demo
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

import hooks  # noqa: E402 — .engine/tools/hooks.py: the SessionStart fail-open harness
from memory import compact, ledger, records  # noqa: E402

ERASURE_LABEL = "engine-erasure"   # the dedicated label a single-purpose erasure PR carries (the observer↔proposer contract)
# The label's own colour + description — canonical here, next to the name. Two creators read them: the proposer
# (which stamps a genuine erasure PR's label) and first-run provisioning (which mirrors this trio, held equal by a
# drift-test). A clear medium blue — deliberate and distinct from the engine label's grey and the reauthoring lavender,
# never an alarm red. The description is count-neutral (a merge can forget a whole batch) and under GitHub's 100-char cap.
ERASURE_LABEL_COLOR = "1d76db"
ERASURE_LABEL_DESCRIPTION = "The engine puts this on a pull request whose merge permanently forgets the notes it lists."

# The committed proposal file the observer reads at the PR's merge tree. A COMMITTED path (NOT under the gitignored
# `.engine/memory/` ledger dir, which could not be committed). The emitter MUST emit `{"targets": [<id>, …], "costs":
# [<plain paraphrase>, …]}` here (a legacy single `{"target": <id>, …}` is still read as a one-note batch) and OWNS
# this file's main-tree lifecycle; the observer commits nothing here and reads ONLY the `targets` ids.
_PROPOSAL_PATH = ".engine/erasures/proposal.json"

# The content-free record-id shape: `records.new_record_id()` mints a uuid4 hex = exactly 32 lowercase hex chars.
# Validating against this exact shape (not a loose hex match) makes a malformed/foreign `target` fail-SAFE.
_RECORD_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")


# --- the GitHub reads (every read fail-OPEN: returns None / [] on any doubt, NEVER raises) -----------------

def _get(gh, path: str):
    """One GET through the injected transport. Returns parsed JSON, or None on ANY failure — an HTTP error
    (status >= 400), a null body, or a transport exception (e.g. GitHub unreachable). Fail-OPEN: the observer
    must never raise into a session start; a read failure simply means "skip" upstream."""
    try:
        status, data = gh._transport("GET", path, None)
    except Exception:  # noqa: BLE001 — a transport fault (unreachable host, etc.) must degrade to "skip", never raise
        return None
    if not isinstance(status, int) or status >= 400 or data is None:
        return None
    return data


def discover_erasure_pr_numbers(gh) -> list:
    """Candidate erasure-PR numbers: CLOSED items carrying the `engine-erasure` label that are PRs (the issues
    endpoint lists PRs too — a PR carries a `pull_request` key). Returns a list of ints (possibly empty); never
    raises. Bounded to the few real erasure PRs by the label filter ("by label/search")."""
    data = _get(gh, f"/repos/{gh.repo}/issues?state=closed&labels={ERASURE_LABEL}&per_page=100")
    if not isinstance(data, list):
        return []
    return [item["number"] for item in data
            if isinstance(item, dict) and "pull_request" in item and isinstance(item.get("number"), int)]


def _is_genuinely_merged(pr) -> bool:
    """True iff this PR object shows a GENUINE merge: a non-null `merged_at` AND a non-empty `merge_commit_sha`.
    A merely-closed (declined / auto-resolved) PR has `merged_at` null — the locked "merge event only" gate; such
    a PR never erases."""
    if not isinstance(pr, dict):
        return False
    merge_sha = pr.get("merge_commit_sha")
    return bool(pr.get("merged_at")) and isinstance(merge_sha, str) and bool(merge_sha)


def _is_record_id(value) -> bool:
    """True iff `value` is exactly the content-free record-id shape (`uuid4().hex` — 32 lowercase hex chars)."""
    return isinstance(value, str) and _RECORD_ID_RE.match(value) is not None


def _read_targets(gh, merge_sha: str) -> list:
    """The content-free target ids, read from the committed proposal file at the IMMUTABLE merge tree
    (`?ref=merge_sha`). Accepts the batch grammar `{"targets": [id, …]}` and a legacy single `{"target": id}` (read
    as a one-element batch). Returns a list of validated record ids, or [] on ANY doubt (fail-SAFE): 404/absent, a
    directory (a list body), a non-`base64` encoding (the `"none"` GitHub returns for >1 MB blobs), a base64/JSON
    decode error, a non-object body, an empty/absent target list, OR any element that is not the content-free
    record-id shape. WHOLE-BATCH REJECT: one malformed element voids the ENTIRE batch — the operator consented to
    the committed list, so a corrupt/foreign element means act on none and resurface next session (the faithful
    generalisation of the single-target `None`-on-doubt; and since the proposer only ever writes valid ids, a
    malformed element implies corruption or tampering — exactly when to erase nothing). For the batch grammar it
    also requires a `costs` list of EQUAL length (a structural consent-integrity check — the operator consents on
    one cost line per target; a `targets`/`costs` mismatch means the committed list and the enumerated body diverged,
    so erase none). It reads only the ids for enactment and the `costs` LENGTH for that check — never a cost line's
    content, never any ledger content."""
    data = _get(gh, f"/repos/{gh.repo}/contents/{_PROPOSAL_PATH}?ref={merge_sha}")
    if not isinstance(data, dict) or data.get("encoding") != "base64":
        return []
    raw = data.get("content")
    if not isinstance(raw, str):
        return []
    try:
        obj = json.loads(base64.b64decode(raw).decode("utf-8"))  # base64 may carry embedded newlines — decode tolerates
    except Exception:  # noqa: BLE001 — a malformed/corrupt proposal is a doubt -> skip (fail-SAFE)
        return []
    if not isinstance(obj, dict):
        return []
    if isinstance(obj.get("targets"), list):
        raw_ids = obj["targets"]
        costs = obj.get("costs")
        if not isinstance(costs, list) or len(costs) != len(raw_ids):
            return []                                            # batch grammar: costs must pin 1:1 with targets
    elif obj.get("target") is not None:
        raw_ids = [obj.get("target")]                            # legacy single-target proposal -> a one-note batch
    else:
        return []
    if not raw_ids or not all(_is_record_id(t) for t in raw_ids):
        return []                                                # whole-batch reject: empty or any invalid id -> none
    return list(raw_ids)


# --- dedup + enactment ------------------------------------------------------------------------------------

def _erased_targets(path: "str | None" = None) -> set:
    """The set of target ids the ledger ALREADY holds an erasure marker for — the cross-session dedup ledger. The
    marker is retained across compaction (the enactment tombstone), so this set persists even after the note is gone."""
    return {r.get(records.TARGET_KEY) for r in ledger.iter_records(path=path)
            if isinstance(r, dict) and r.get("kind") == records.ERASURE_KIND}


def _already_enacted(target: str, *, path: "str | None" = None) -> bool:
    """True iff an erasure marker already targets `target` — dedup on the TARGET ID ALONE (a content-free id is
    unique; once any merge authorised erasing it, a re-merged PR must not re-mint or re-fire the heads-up)."""
    return target in _erased_targets(path)


def enact_from_merged_prs(gh, *, path: "str | None" = None) -> list:
    """Discover merged `engine-erasure` PRs, enact each not-yet-enacted target in each PR's committed batch, and
    return the PR numbers NEWLY enacted THIS run (for the one-time heads-up). Pure orchestration over the fail-open
    reads + the enactment minter. A merged batch PR mints ONE singular marker per target under the SHARED merge SHA:
    the ledger's existing targets are read ONCE into `seen` and each mint adds to it, so a partial run (mint some,
    crash, re-run) re-mints only the missing targets, and two PRs naming one target — or a re-merge — never
    double-mint. A PR is reported enacted iff at least ONE of its targets was newly minted. Never raises."""
    seen = _erased_targets(path)
    enacted: list = []
    for number in discover_erasure_pr_numbers(gh):
        pr = _get(gh, f"/repos/{gh.repo}/pulls/{number}")
        if not _is_genuinely_merged(pr):
            continue
        merge_sha = pr["merge_commit_sha"]
        newly = False
        for target in _read_targets(gh, merge_sha):
            if target in seen:
                continue
            if compact.enact_erasure(target, merge_sha, path=path) is not None:
                seen.add(target)
                newly = True
        if newly:
            enacted.append(number)
    return enacted


# --- the SessionStart hook (fail-open; one-time plain-language heads-up) -----------------------------------

def _reader():
    """A GitHub reader over the operator's `gh` token, or None when the repo/token can't be resolved (a degraded
    host — proceed silently). Reuses boot's resolvers + `telemetry`'s 2-tuple transport; lazy-imported so the
    cold-start load path stays light until the observer actually reads."""
    import boot       # noqa: E402 — lazy: keep boot's heavy import graph off the module-load path
    import telemetry  # noqa: E402 — its GitHubIssues exposes the injectable 2-tuple `_transport` (= `_http`)
    repo = boot.repo_slug()
    token = boot.gh_token()
    if not repo or not token:
        return None
    return telemetry.GitHubIssues(repo, token)


def _heads_up(pr_numbers: list, note_count: "int | None" = None) -> str:
    """The model-facing relay for a NEW enactment — plain language, the operator's chosen one-time heads-up. Names
    the PR(s) the operator merged (operator-recognisable, immutable) and NEVER the note's content. `note_count` is
    how many notes those merges scheduled (a single PR may clear a whole batch); it defaults to the PR count for a
    legacy one-note-per-PR merge."""
    refs = ", ".join(f"#{n}" for n in sorted(set(pr_numbers)))
    pr_count = len(set(pr_numbers))
    pr_phrase = "a single-purpose erasure pull request" if pr_count == 1 else "single-purpose erasure pull requests"
    n = pr_count if note_count is None else note_count
    notes = "one remembered note is" if n == 1 else f"{n} remembered notes are"
    return (
        f"INFORM THE USER, in plain language (they asked to be told once): because they merged {pr_phrase} "
        f"({refs}), {notes} now scheduled to be permanently erased at the next memory tidy — the one action on "
        f"their memory that cannot be undone, happening only because they merged it.")


def _session_start_handler(payload) -> dict:
    """Memory's cross-session erasure OBSERVER at SessionStart. Fail-open throughout: resolve a reader, enact any
    merged erasure PR not yet acted on, and on a NEW enactment relay ONE plain-language heads-up; otherwise proceed
    silently. Any doubt or fault -> a silent proceed (the session is never blocked; the observer retries next
    session). `payload` is unused (the observer reads GitHub, not the event)."""
    try:
        gh = _reader()
        if gh is None:
            return hooks.proceed()                 # no repo/token -> degraded host, silent
        before = len(_erased_targets())            # how many targets were already scheduled...
        enacted = enact_from_merged_prs(gh)
        if enacted:
            return hooks.inject(_heads_up(enacted, len(_erased_targets()) - before))  # ...so the delta = notes newly scheduled
    except Exception:  # noqa: BLE001 — fail-open: a fault here must never strand the session start
        return hooks.proceed()
    return hooks.proceed()


# --- operator demonstration (REAL observer logic; only the GitHub transport is stubbed) --------------------
# A walkthrough on a THROWAWAY practice cabinet. It runs the REAL discovery -> genuine-merge -> read-targets@tree ->
# dedup -> enact -> compact path; only the GitHub network is a stub at the injectable seam. It proves: the engine
# acts on the PR you MERGED (not one merely closed) and on what you COMMITTED (not the editable PR body), erases the
# WHOLE batch that one merge authorised (here two notes) in a single tidy, leaves an un-authorised note alone, and
# never repeats. Two notes here because the demo plants two; one merge clears the whole batch. Re-run to re-read:
#     uv run --directory .engine --frozen -- python tools/memory/erasure_observer.py demo
_DEMO_SESSION = "session-observer"
_DEMO_KEEP_TEXT = "Decided the harbor festival keeps its Saturday fireworks. KEEP-THIS-NOTE."
_DEMO_KEEP_WORD = "fireworks"
_DEMO_GONE1_TEXT = "Withdrawn idea: move the depot onto the floodplain. ERASE-THIS-NOTE."
_DEMO_GONE1_WORD = "floodplain"
_DEMO_GONE2_TEXT = "Withdrawn idea: pave the millpond for extra parking. ERASE-THIS-TOO."
_DEMO_GONE2_WORD = "millpond"
_DEMO_MERGED_PR = 7         # the merged single-purpose erasure PR (its committed proposal names BOTH withdrawn notes)
_DEMO_CLOSED_PR = 8         # a control: CLOSED but NOT merged -> the observer must leave it alone
_DEMO_MERGE_SHA = "a1b2c3d4e5f600000000000000000000deadbeef"


class _FakeGH:
    """A stand-in GitHub reader for the demo/tests: a fixed repo + an injected transport. Lets the REAL observer
    logic run fully offline ([[demo-must-exercise-real-logic]]) — only the network is faked."""

    def __init__(self, transport, *, repo="your-org/your-project"):
        self.repo = repo
        self._transport = transport


def _stub_transport(*, target_id: str = None, targets: "list | None" = None, body_id: str,
                    merged_sha: str = _DEMO_MERGE_SHA):
    """Answer the three GETs the observer makes: the by-label discovery (a merged PR + a closed-unmerged control,
    both labelled), each `/pulls/{n}`, and the proposal read at the merged PR's tree. The merged PR's BODY names
    `body_id` (an un-authorised note) while the committed proposal names the authorised target(s) — so the demo can
    prove the observer binds to the merge tree, not the editable body. Pass `targets=[…]` for the batch grammar
    `{"targets": […], "costs": […]}`, or `target_id=…` for a legacy single `{"target": …}` (back-compat)."""
    if targets is not None:
        payload = {"targets": list(targets), "costs": ["a withdrawn idea you decided to drop"] * len(targets)}
    else:
        payload = {"target": target_id, "cost": "a withdrawn idea you decided to drop"}
    proposal = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")

    def transport(method, path, body):
        if "/issues?" in path:                                   # the by-label discovery (closed items)
            return 200, [{"number": _DEMO_MERGED_PR, "pull_request": {}},
                         {"number": _DEMO_CLOSED_PR, "pull_request": {}}]
        m = re.search(r"/pulls/(\d+)", path)
        if m and int(m.group(1)) == _DEMO_MERGED_PR:
            return 200, {"number": _DEMO_MERGED_PR, "merged_at": "2026-06-21T00:00:00Z",
                         "merge_commit_sha": merged_sha,
                         "body": f"Erase the withdrawn note. (an editable body that names {body_id})"}
        if m and int(m.group(1)) == _DEMO_CLOSED_PR:
            return 200, {"number": _DEMO_CLOSED_PR, "merged_at": None, "merge_commit_sha": None,
                         "body": "a different change that was closed without merging"}
        if "/contents/" in path and f"ref={merged_sha}" in path:
            return 200, {"content": proposal, "encoding": "base64"}
        return 404, None

    return transport


def _plant(text: str) -> str:
    """Plant one real, always-live note through the live factory; return its content-free id."""
    from memory import legacy_shapes as _legacy
    rec = _legacy.episodic(_DEMO_SESSION, "decision", text, "demo-batch")
    rec.pop(records.BATCH_KEY, None)               # always-live (not a crashed-pass orphan)
    ledger.append(rec)
    return rec[records.RECORD_ID_KEY]


def _found(word: str) -> int:
    from memory import index
    return len(index.query(word).records)


def _rebuild() -> None:
    from memory import index
    index.rebuild()


def _slips() -> int:
    return sum(1 for r in ledger.iter_records() if isinstance(r, dict) and r.get("kind") == records.ERASURE_KIND)


def _has_slip_for(target_id: str) -> bool:
    return any(isinstance(r, dict) and r.get("kind") == records.ERASURE_KIND
               and r.get(records.TARGET_KEY) == target_id for r in ledger.iter_records())


def _present(record_id: str) -> bool:
    return any(isinstance(r, dict) and r.get(records.RECORD_ID_KEY) == record_id
               for r in ledger.iter_records())


def _snippet(text, width: int = 66) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _demo() -> int:
    import tempfile

    print("=" * 92)
    print("MEMORY — the engine acts on the erasure pull request YOU merged: turning your consent into the act (practice)")
    print("=" * 92)
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["ENGINE_MEMORY_DIR"] = tmp          # the throwaway cabinet
        try:
            ok = _demo_body()
        finally:
            os.environ.pop("ENGINE_MEMORY_DIR", None)

    print("\n" + "-" * 92)
    print("What this just proved: when you MERGE a single-purpose erasure pull request, the engine schedules exactly")
    print("the batch of notes you authorised for permanent erasure — here two, cleared in one merge — and it acts on")
    print("the pull request you MERGED (not one merely closed) and on what you COMMITTED (not the pull-request text,")
    print("which can be edited after the fact). It tells you once, then never again. This is the ONE thing the engine")
    print("can do to your memory that cannot be undone, and it happens ONLY because you merged that pull request. An")
    print("un-authorised note is left alone. A real batch bundles what has earned erasure — old crash-duplicate")
    print("leftovers and the raw turn-by-turn notes of sessions whose summaries have settled (the bulk source) — so one")
    print("merge can clear a whole backlog at once; here the demo planted two.")
    print("If GitHub is ever unreachable, the engine simply carries on and tries again next session — nothing breaks.")
    print("That was a PRACTICE cabinet, thrown away.")
    return 0 if ok else 1


def _demo_body() -> bool:
    keep_id = _plant(_DEMO_KEEP_TEXT)
    gone1_id = _plant(_DEMO_GONE1_TEXT)
    gone2_id = _plant(_DEMO_GONE2_TEXT)
    _rebuild()
    gone_ids = {gone1_id, gone2_id}
    # The merged PR's committed proposal names BOTH withdrawn notes (a batch); its editable BODY names the kept note.
    gh = _FakeGH(_stub_transport(targets=[gone1_id, gone2_id], body_id=keep_id))

    # --- PART 1 ------------------------------------------------------------------------------------------
    print("\nPART 1 — three real notes are on file, all findable")
    print("-" * 92)
    found_gone1, found_gone2, found_keep = _found(_DEMO_GONE1_WORD), _found(_DEMO_GONE2_WORD), _found(_DEMO_KEEP_WORD)
    print(f'  the two notes the merged pull request authorises erasing: "{_DEMO_GONE1_WORD}" -> {found_gone1}, "{_DEMO_GONE2_WORD}" -> {found_gone2}')
    print(f'  the note you did NOT authorise: search "{_DEMO_KEEP_WORD}" -> found {found_keep}')
    part1 = found_gone1 == 1 and found_gone2 == 1 and found_keep == 1
    print(f"  => {'all three notes are on file and findable.' if part1 else '!!! a note is missing at the start'}")

    # --- PART 2 ------------------------------------------------------------------------------------------
    print(f"\nPART 2 — the engine reads GitHub: it acts on the MERGED pull request (#{_DEMO_MERGED_PR}), not the one")
    print(f"         merely CLOSED (#{_DEMO_CLOSED_PR}) — and one merge schedules the WHOLE batch")
    print("-" * 92)
    enacted = enact_from_merged_prs(gh)              # the REAL observer, against the stubbed GitHub
    print(f"  pull requests the engine acted on this session: {enacted}   (it ignored the closed-not-merged one)")
    print(f"  permanent-erase authorisations now on file: {_slips()}   (one per note in the batch, one shared merge)")
    print("  the one-time heads-up the engine would show you:")
    if enacted:
        print(f"    \"{_heads_up(enacted, _slips())[len('INFORM THE USER, in plain language (they asked to be told once): '):]}\"")
    part2 = enacted == [_DEMO_MERGED_PR] and _slips() == 2
    print(f"  => {'it acted only on the pull request you merged, scheduling both notes it named.' if part2 else '!!! it acted on the wrong pull request, or scheduled the wrong count'}")

    # --- PART 3 ------------------------------------------------------------------------------------------
    print("\nPART 3 — it acted on what you COMMITTED, not on the pull-request text (which can be edited later)")
    print("-" * 92)
    scheduled_batch = _has_slip_for(gone1_id) and _has_slip_for(gone2_id)
    scheduled_keep = _has_slip_for(keep_id)
    print(f'  the merged pull request\'s editable text named the un-authorised note ("{_snippet(_DEMO_KEEP_TEXT)}")')
    print(f"  the notes actually scheduled are the ones named in the committed file: {'both authorised notes' if scheduled_batch else 'NO'}")
    print(f"  the note named only in the editable text was left alone: {'yes' if not scheduled_keep else 'NO (scheduled it!)'}")
    part3 = scheduled_batch and not scheduled_keep
    print(f"  => {'it bound to what you committed, not to text that can be edited after the fact.' if part3 else '!!! it followed the editable text instead of the committed file'}")

    # --- PART 4 ------------------------------------------------------------------------------------------
    print("\nPART 4 — the tidy erases BOTH in one swap; the un-authorised note is untouched; re-checking changes nothing")
    print("-" * 92)
    report = compact.compact()
    batch_gone = not _present(gone1_id) and not _present(gone2_id)
    found_keep_after = _found(_DEMO_KEEP_WORD)
    enacted_again = enact_from_merged_prs(gh)        # the SAME merged PR, a later session -> dedup, no re-fire
    print(f'  the authorised notes: "{_DEMO_GONE1_WORD}" -> {_found(_DEMO_GONE1_WORD)}, "{_DEMO_GONE2_WORD}" -> {_found(_DEMO_GONE2_WORD)}   (both physically gone: {"yes" if batch_gone else "NO"})')
    print(f'  the un-authorised note: search "{_DEMO_KEEP_WORD}" -> found {found_keep_after}   (untouched)')
    print(f"  re-checking GitHub next session acted on: {enacted_again}   (none — already done; you are NOT told again)")
    part4 = (batch_gone and report.get("erased") == 2 and found_keep_after == 1
             and enacted_again == [] and _slips() == 2)
    print(f"  => {'the whole batch erased once and only once; the un-authorised note survived; no second heads-up.' if part4 else '!!! the wrong note changed, it repeated, or it re-fired the notice'}")

    return part1 and part2 and part3 and part4


def main(argv: list) -> int:
    cmd = argv[0] if argv else "demo"
    if cmd == "session-start":
        return hooks.run_hook("SessionStart", _session_start_handler)
    if cmd == "demo":
        return _demo()
    print(f"usage: erasure_observer.py [session-start|demo]\nunknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
